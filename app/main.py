import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from groq import AsyncGroq
from pydantic import BaseModel, Field

from app import database
from app.agents import build_workflow
from app.monitoring.agent_monitor import get_agent_health
from app.monitoring.diagnostic_agent import build_diagnostic_workflow
from app.monitoring.service import (
    DEFAULT_ALERT_SETTINGS,
    monitor_loop,
    monitor_service,
)
from app.monitoring.system_monitor import application_uptime_seconds

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / "app" / ".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agentos")

MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
API_KEY = os.getenv("GROQ_API_KEY")
groq_client: AsyncGroq | None = AsyncGroq(api_key=API_KEY) if API_KEY else None


def save_groq_config_to_env(api_key: str, model: str) -> None:
    for target in [BASE_DIR / ".env", BASE_DIR / "app" / ".env"]:
        try:
            lines = []
            found_key = False
            found_model = False
            if target.exists():
                for line in target.read_text(encoding="utf-8").splitlines():
                    if line.startswith("GROQ_API_KEY="):
                        lines.append(f"GROQ_API_KEY={api_key}")
                        found_key = True
                    elif line.startswith("GROQ_MODEL="):
                        lines.append(f"GROQ_MODEL={model}")
                        found_model = True
                    else:
                        lines.append(line)
            if not found_key:
                lines.append(f"GROQ_API_KEY={api_key}")
            if not found_model:
                lines.append(f"GROQ_MODEL={model}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            logger.exception("Failed to write env file to %s", target)


STAGES = [
    ("planner", "Plan the request"),
    ("researcher", "Analyze relevant information"),
    ("executor", "Prepare a response"),
    ("verifier", "Check the result"),
    ("communicator", "Deliver the answer"),
]
AGENTS = [
    {"id": "planner", "name": "Planner", "role": "Breaks requests into clear steps"},
    {"id": "researcher", "name": "Researcher", "role": "Analyzes information using model knowledge"},
    {"id": "executor", "name": "Executor", "role": "Prepares permitted outputs; no external tools connected"},
    {"id": "verifier", "name": "Verifier", "role": "Checks quality and unsupported claims"},
    {"id": "communicator", "name": "Communicator", "role": "Delivers the final response"},
]
PREFERENCE_KEYS = {"display_name", "theme", "confirm_sensitive_actions"}
HISTORY_PERIODS = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}


class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=6000)
    conversation_id: int | None = None


class ConversationInput(BaseModel):
    title: str = Field(default="New conversation", min_length=1, max_length=120)


class ApprovalInput(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")


class PreferenceInput(BaseModel):
    key: str
    value: Any


class AlertSettingsInput(BaseModel):
    cpu_percent: float = Field(ge=1, le=100)
    memory_percent: float = Field(ge=1, le=100)
    disk_percent: float = Field(ge=1, le=100)
    api_latency_ms: float = Field(ge=1, le=60000)
    websocket_disconnects: int = Field(ge=1, le=1000)
    notifications_enabled: bool


class GroqConfigInput(BaseModel):
    api_key: str = Field(min_length=1)
    model: str | None = None


class ConnectionManager:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.connections:
            self.connections.discard(websocket)
            database.record_websocket_event("disconnected")

    async def broadcast(self, event: dict[str, Any]) -> None:
        disconnected = []
        for connection in tuple(self.connections):
            try:
                await connection.send_json(event)
            except (OSError, WebSocketDisconnect, RuntimeError):
                disconnected.append(connection)
        for connection in disconnected:
            self.disconnect(connection)


manager = ConnectionManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.initialize_database()
    stop_event = asyncio.Event()
    monitor_task = asyncio.create_task(
        monitor_loop(lambda: groq_client, lambda: len(manager.connections), stop_event)
    )
    try:
        yield
    finally:
        stop_event.set()
        await monitor_task


app = FastAPI(title="AgentOS 2.0", version="2.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def monitor_api_requests(request: Request, call_next):
    if not request.url.path.startswith("/api/") or request.url.path.startswith("/api/monitor/"):
        return await call_next(request)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as error:
        latency_ms = (time.perf_counter() - started) * 1000
        route = getattr(request.scope.get("route"), "path", "unmatched")
        try:
            database.record_api_metric(route, 500, latency_ms)
            database.record_application_error(
                route, f"{type(error).__name__}: {str(error)[:1500]}"
            )
        except Exception:
            logger.exception("Could not persist API failure telemetry")
        logger.exception("Unhandled API error for %s", route)
        raise

    route = getattr(request.scope.get("route"), "path", "unmatched")
    latency_ms = (time.perf_counter() - started) * 1000
    try:
        database.record_api_metric(route, response.status_code, latency_ms)
        if response.status_code >= 500:
            database.record_application_error(
                route, f"HTTP {response.status_code} response."
            )
    except Exception:
        logger.exception("Could not persist API request telemetry")
    return response


@app.get("/")
def home() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "provider": "Groq",
        "model": MODEL,
        "configured": groq_client is not None,
        "uptime_seconds": application_uptime_seconds(),
    }


@app.get("/api/groq/config")
def get_groq_configuration() -> dict[str, Any]:
    masked = ""
    if API_KEY:
        masked = f"{API_KEY[:4]}...{API_KEY[-4:]}" if len(API_KEY) > 8 else "configured"
    return {
        "configured": groq_client is not None,
        "model": MODEL,
        "masked_key": masked,
    }


@app.get("/api/groq/models")
async def get_groq_models() -> dict[str, Any]:
    client = groq_client
    if client is None:
        return {
            "configured": False,
            "models": [
                "openai/gpt-oss-120b",
                "openai/gpt-oss-20b",
                "qwen/qwen3.8-27b",
                "allam-2-7b",
            ],
        }
    try:
        response = await client.models.list(timeout=8.0)
        models = [
            m.id
            for m in response.data
            if not m.id.startswith("whisper")
            and not m.id.startswith("meta-llama/llama-prompt-guard")
        ]
        return {
            "configured": True,
            "models": models,
        }
    except Exception as error:
        logger.exception("Failed to fetch Groq models")
        return {
            "configured": True,
            "error": str(error),
            "models": [
                "openai/gpt-oss-120b",
                "openai/gpt-oss-20b",
                "qwen/qwen3.8-27b",
            ],
        }


@app.post("/api/groq/configure")
async def configure_groq(payload: GroqConfigInput) -> dict[str, Any]:
    global API_KEY, MODEL, groq_client
    new_key = payload.api_key.strip()
    new_model = (payload.model or MODEL or "openai/gpt-oss-120b").strip()

    test_client = AsyncGroq(api_key=new_key)
    try:
        models_resp = await test_client.models.list(timeout=8.0)
        available_model_ids = {m.id for m in models_resp.data}
    except Exception as error:
        logger.warning("Groq configuration verification failed: %s", error)
        raise HTTPException(
            status_code=400,
            detail=f"Failed to connect to Groq with the provided API key: {error}",
        )

    if new_model not in available_model_ids and not any(new_model in mid for mid in available_model_ids):
        for candidate in ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]:
            if candidate in available_model_ids:
                new_model = candidate
                break

    API_KEY = new_key
    MODEL = new_model
    groq_client = test_client

    os.environ["GROQ_API_KEY"] = API_KEY
    os.environ["GROQ_MODEL"] = MODEL
    save_groq_config_to_env(API_KEY, MODEL)

    monitor_service.reset_ai_probe()
    snapshot = await _monitoring_snapshot(force_ai_probe=True)

    await manager.broadcast({
        "type": "groq_configured",
        "configured": True,
        "model": MODEL,
        "snapshot": snapshot,
    })

    return {
        "status": "ok",
        "message": "Groq connected successfully!",
        "configured": True,
        "model": MODEL,
    }


@app.get("/api/agents")
def get_agents() -> list[dict[str, str]]:
    return AGENTS


@app.get("/api/conversations")
def get_conversations() -> list[dict[str, Any]]:
    return database.list_conversations()


@app.post("/api/conversations", status_code=201)
def post_conversation(payload: ConversationInput) -> dict[str, Any]:
    return database.create_conversation(payload.title.strip() or "New conversation")


@app.get("/api/conversations/{conversation_id}/messages")
def get_messages(conversation_id: int) -> list[dict[str, Any]]:
    if not database.get_conversation(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return database.list_messages(conversation_id)


@app.delete("/api/conversations/{conversation_id}", status_code=204)
async def remove_conversation(conversation_id: int) -> None:
    if not database.delete_conversation(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    await manager.broadcast(
        {"type": "conversation_deleted", "conversation_id": conversation_id}
    )


@app.delete("/api/history", status_code=204)
async def remove_history() -> None:
    database.clear_history()
    await manager.broadcast({"type": "history_cleared"})


@app.post("/api/chat")
async def chat(payload: ChatInput) -> dict[str, Any]:
    if groq_client is None:
        raise HTTPException(
            status_code=503,
            detail="Groq is not configured. Set GROQ_API_KEY in Settings or your environment and restart AgentOS.",
        )

    user_message = payload.message.strip()
    if not user_message:
        raise HTTPException(status_code=422, detail="Message cannot be blank.")
    conversation = (
        database.get_conversation(payload.conversation_id)
        if payload.conversation_id is not None
        else None
    )
    if payload.conversation_id is not None and conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if conversation is None:
        title = user_message[:52].strip()
        if len(user_message) > 52:
            title += "..."
        conversation = database.create_conversation(title or "New conversation")

    conversation_id = conversation["id"]
    database.add_message(conversation_id, "user", user_message)
    task_ids = database.create_tasks(conversation_id, STAGES)
    task_lookup = {agent: task_id for (agent, _), task_id in zip(STAGES, task_ids)}
    workflow = build_workflow(groq_client, MODEL)
    current_agent = "planner"
    agent_runs: dict[str, int] = {}
    agent_started: dict[str, float] = {}
    agent_runs[current_agent] = database.start_agent_run(
        conversation_id, current_agent, dict(STAGES)[current_agent]
    )
    agent_started[current_agent] = time.perf_counter()
    database.update_task(task_lookup[current_agent], "running", "Working on the request.")
    database.record_activity(conversation_id, current_agent, "Started planning", "running")
    await manager.broadcast(
        {
            "type": "agent_progress",
            "conversation_id": conversation_id,
            "agent": current_agent,
            "status": "running",
            "message": "Planner is breaking down the request.",
        }
    )

    state: dict[str, Any] = {"request": user_message}
    try:
        async for update in workflow.astream(state, stream_mode="updates"):
            for node_name, values in update.items():
                if node_name == "risk_gate":
                    state.update(values)
                    if values.get("approval_required"):
                        executor_id = task_lookup["executor"]
                        database.update_task(
                            executor_id,
                            "pending_approval",
                            "Sensitive action requested. No action was performed.",
                            requires_approval=True,
                        )
                        database.record_activity(
                            conversation_id,
                            "executor",
                            "Held for human approval; no action performed",
                            "pending_approval",
                        )
                        await manager.broadcast(
                            {
                                "type": "agent_progress",
                                "conversation_id": conversation_id,
                                "agent": "executor",
                                "status": "pending_approval",
                                "message": "Executor is waiting for your approval. No action was performed.",
                            }
                        )
                        current_agent = "verifier"
                        run_id = await _start_agent(conversation_id, "verifier", task_lookup)
                        if run_id is not None:
                            agent_runs["verifier"] = run_id
                            agent_started["verifier"] = time.perf_counter()
                    else:
                        current_agent = "executor"
                        run_id = await _start_agent(conversation_id, "executor", task_lookup)
                        if run_id is not None:
                            agent_runs["executor"] = run_id
                            agent_started["executor"] = time.perf_counter()
                    continue
                state.update(values)
                completed_task = task_lookup[node_name]
                run_id = agent_runs.get(node_name)
                if run_id is not None:
                    database.finish_agent_run(
                        run_id,
                        "completed",
                        (time.perf_counter() - agent_started[node_name]) * 1000,
                    )
                database.update_task(completed_task, "completed", "Step completed.")
                database.record_activity(
                    conversation_id, node_name, "Completed workflow step", "completed"
                )
                await manager.broadcast(
                    {
                        "type": "agent_progress",
                        "conversation_id": conversation_id,
                        "agent": node_name,
                        "status": "completed",
                        "message": f"{node_name.title()} completed its step.",
                    }
                )
                next_agent = _next_agent(node_name)
                if next_agent:
                    current_agent = next_agent
                    run_id = await _start_agent(conversation_id, next_agent, task_lookup)
                    if run_id is not None:
                        agent_runs[next_agent] = run_id
                        agent_started[next_agent] = time.perf_counter()
        if not state.get("response"):
            raise RuntimeError("The agent workflow did not produce a final response.")
        database.add_message(conversation_id, "assistant", state["response"])
        await manager.broadcast(
            {
                "type": "workflow_completed",
                "conversation_id": conversation_id,
                "message": "The workflow completed.",
            }
        )
        return {
            "conversation_id": conversation_id,
            "response": state["response"],
            "approval_required": bool(state.get("approval_required")),
        }
    except Exception as error:
        logger.exception("Workflow failed for conversation %s", conversation_id)
        failed_run = agent_runs.get(current_agent)
        if failed_run is not None:
            database.finish_agent_run(
                failed_run,
                "failed",
                (time.perf_counter() - agent_started[current_agent]) * 1000,
                f"{type(error).__name__}: {error}",
            )
        try:
            database.record_application_error(
                f"agent:{current_agent}", f"{type(error).__name__}: {str(error)[:1500]}"
            )
        except Exception:
            logger.exception("Could not persist agent failure telemetry")
        for agent, _ in STAGES:
            task = database.get_task(task_lookup[agent])
            if task and task["status"] in {"pending", "running"}:
                database.update_task(
                    task_lookup[agent],
                    "failed" if agent == current_agent else "cancelled",
                    "Workflow failed before this step completed.",
                )
        database.record_activity(
            conversation_id, current_agent, "Workflow failed", "failed"
        )
        await manager.broadcast(
            {
                "type": "workflow_error",
                "conversation_id": conversation_id,
                "message": "The workflow could not be completed. No external action was performed.",
            }
        )
        raise HTTPException(
            status_code=502,
            detail="The agent workflow failed. Check the server logs and try again.",
        ) from error


def _next_agent(agent: str) -> str | None:
    if agent == "planner":
        return "researcher"
    if agent == "executor":
        return "verifier"
    if agent == "verifier":
        return "communicator"
    return None


async def _start_agent(
    conversation_id: int, agent: str, task_lookup: dict[str, int]
) -> int | None:
    task = database.get_task(task_lookup[agent])
    if task and task["status"] == "pending":
        run_id = database.start_agent_run(
            conversation_id, agent, dict(STAGES)[agent]
        )
        database.update_task(task_lookup[agent], "running", "Working on the request.")
        database.record_activity(
            conversation_id, agent, "Started workflow step", "running"
        )
        await manager.broadcast(
            {
                "type": "agent_progress",
                "conversation_id": conversation_id,
                "agent": agent,
                "status": "running",
                "message": f"{agent.title()} is working.",
            }
        )
        return run_id
    return None


@app.get("/api/tasks")
def get_tasks(conversation_id: int | None = Query(default=None)) -> list[dict[str, Any]]:
    return database.list_tasks(conversation_id)


@app.get("/api/approvals")
def get_approvals() -> list[dict[str, Any]]:
    return [
        task
        for task in database.list_tasks()
        if task["requires_approval"] and task["status"] in {"pending_approval", "approved", "rejected"}
    ]


@app.post("/api/approvals/{task_id}")
async def decide_approval(task_id: int, payload: ApprovalInput) -> dict[str, Any]:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if not task["requires_approval"] or task["status"] != "pending_approval":
        raise HTTPException(status_code=409, detail="This task is not awaiting approval.")
    status = "approved" if payload.decision == "approve" else "rejected"
    detail = (
        "Authorization recorded. No external action was executed because no action tools are connected."
        if status == "approved"
        else "Request rejected. No external action was performed."
    )
    database.update_task(task_id, status, detail, requires_approval=True)
    database.record_activity(
        task["conversation_id"], "user", f"Approval {payload.decision}d", status
    )
    await manager.broadcast(
        {
            "type": "approval_updated",
            "conversation_id": task["conversation_id"],
            "task_id": task_id,
            "status": status,
        }
    )
    return {"task_id": task_id, "status": status, "detail": detail}


@app.get("/api/activity")
def get_activity(limit: int = Query(default=100, ge=1, le=250)) -> list[dict[str, Any]]:
    return database.list_activity(limit)


@app.get("/api/preferences")
def get_preferences() -> dict[str, Any]:
    return database.get_preferences()


@app.put("/api/preferences")
def put_preference(payload: PreferenceInput) -> dict[str, Any]:
    if payload.key not in PREFERENCE_KEYS:
        raise HTTPException(status_code=422, detail="Unsupported preference.")
    if payload.key == "theme" and payload.value not in {"dark", "light"}:
        raise HTTPException(status_code=422, detail="Theme must be dark or light.")
    if payload.key == "confirm_sensitive_actions" and not isinstance(payload.value, bool):
        raise HTTPException(status_code=422, detail="Preference value must be a boolean.")
    if payload.key == "display_name" and (
        not isinstance(payload.value, str) or len(payload.value) > 60
    ):
        raise HTTPException(status_code=422, detail="Display name must be 60 characters or fewer.")
    database.save_preference(payload.key, payload.value)
    return {"key": payload.key, "value": payload.value}


async def _monitoring_snapshot(force_ai_probe: bool = False) -> dict[str, Any]:
    snapshot = await monitor_service.collect(
        lambda: groq_client, len(manager.connections), force_ai_probe=force_ai_probe
    )
    snapshot["alerts"] = {
        "active": len(database.list_alerts(active_only=True, limit=500))
    }
    return snapshot


@app.get("/api/monitor/overview")
async def monitor_overview() -> dict[str, Any]:
    return await _monitoring_snapshot()


@app.get("/api/monitor/history")
def monitor_history(
    period: Literal["1h", "6h", "24h", "7d"] = Query(default="24h"),
    limit: int = Query(default=300, ge=1, le=1000),
) -> dict[str, Any]:
    since = (datetime.now(UTC) - HISTORY_PERIODS[period]).isoformat()
    return {
        "period": period,
        "samples": database.list_monitor_samples(since, limit),
    }


@app.get("/api/monitor/agents")
def monitor_agents() -> list[dict[str, Any]]:
    return get_agent_health()


@app.get("/api/monitor/agents/history")
def monitor_agent_history(
    period: Literal["1h", "6h", "24h", "7d"] = Query(default="24h"),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    since = (datetime.now(UTC) - HISTORY_PERIODS[period]).isoformat()
    return database.list_agent_runs(since, limit)


@app.get("/api/monitor/alerts")
def monitor_alerts(active_only: bool = False) -> list[dict[str, Any]]:
    return database.list_alerts(active_only=active_only)


@app.post("/api/monitor/alerts/{alert_id}/acknowledge")
async def acknowledge_monitor_alert(alert_id: int) -> dict[str, Any]:
    if not database.update_alert_status(alert_id, "acknowledged"):
        raise HTTPException(
            status_code=404, detail="Active alert not found or already acknowledged."
        )
    await manager.broadcast(
        {"type": "monitor_alert_acknowledged", "alert_id": alert_id}
    )
    return {"alert_id": alert_id, "status": "acknowledged"}


@app.get("/api/monitor/settings")
def monitor_settings() -> dict[str, Any]:
    return database.get_monitor_settings(DEFAULT_ALERT_SETTINGS)


@app.put("/api/monitor/settings")
def update_monitor_settings(payload: AlertSettingsInput) -> dict[str, Any]:
    values = payload.model_dump()
    for key, value in values.items():
        database.save_monitor_setting(key, value)
    return database.get_monitor_settings(DEFAULT_ALERT_SETTINGS)


@app.get("/api/monitor/errors")
def monitor_errors(
    period: Literal["1h", "6h", "24h", "7d"] = Query(default="24h"),
) -> list[dict[str, Any]]:
    since = (datetime.now(UTC) - HISTORY_PERIODS[period]).isoformat()
    return database.list_application_errors(since)


@app.get("/api/monitor/diagnostics")
def monitor_diagnostics() -> list[dict[str, Any]]:
    return database.list_diagnostic_reports()


@app.post("/api/monitor/diagnose")
async def run_system_diagnosis() -> dict[str, Any]:
    if groq_client is None:
        raise HTTPException(
            status_code=503,
            detail="Diagnostics require a configured Groq API key.",
        )
    snapshot = await _monitoring_snapshot(force_ai_probe=True)
    since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    snapshot["recent_errors"] = database.list_application_errors(since, limit=30)
    snapshot["recent_alerts"] = database.list_alerts(limit=30)
    try:
        result = await build_diagnostic_workflow(groq_client, MODEL).ainvoke(
            {"snapshot": snapshot}
        )
        report = result["report"]
        report_id = database.save_diagnostic_report(report, snapshot)
        database.record_activity(
            None, "diagnostic", "Generated system diagnostic report", "completed"
        )
        return {
            "id": report_id,
            "report": report,
            "created_at": database.now(),
        }
    except Exception as error:
        logger.exception("System diagnostic workflow failed")
        try:
            database.record_application_error(
                "diagnostic", f"{type(error).__name__}: {str(error)[:1500]}"
            )
        except Exception:
            logger.exception("Could not persist diagnostic failure telemetry")
        raise HTTPException(
            status_code=502,
            detail="System diagnosis failed. Check the server logs and try again.",
        ) from error


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    database.record_websocket_event("connected")
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
