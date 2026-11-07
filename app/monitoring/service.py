import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from app import database
from app.monitoring.ai_monitor import probe_groq
from app.monitoring.agent_monitor import get_agent_health
from app.monitoring.api_monitor import get_api_health
from app.monitoring.database_monitor import check_database_health
from app.monitoring.system_monitor import collect_system_metrics

logger = logging.getLogger("agentos.monitoring")

DEFAULT_ALERT_SETTINGS: dict[str, Any] = {
    "cpu_percent": 85,
    "memory_percent": 85,
    "disk_percent": 90,
    "api_latency_ms": 1500,
    "websocket_disconnects": 5,
    "notifications_enabled": True,
}


class MonitorService:
    def __init__(self) -> None:
        self.ai_status: dict[str, Any] = {
            "status": "checking",
            "available": False,
            "latency_ms": None,
            "error": None,
        }
        self.last_ai_probe = 0.0
        self.sample_lock = asyncio.Lock()

    async def probe_ai(self, client, force: bool = False) -> dict[str, Any]:
        actual_client = client() if callable(client) else client
        if force or time.monotonic() - self.last_ai_probe >= 60:
            self.ai_status = await probe_groq(actual_client)
            self.last_ai_probe = time.monotonic()
            if self.ai_status["status"] == "unavailable":
                database.record_application_error(
                    "groq_probe", self.ai_status["error"] or "Groq API unavailable."
                )
        return dict(self.ai_status)

    def reset_ai_probe(self) -> None:
        self.last_ai_probe = 0.0

    async def collect(
        self, client, websocket_connections: int, force_ai_probe: bool = False
    ) -> dict[str, Any]:
        async with self.sample_lock:
            actual_client = client() if callable(client) else client
            system = await asyncio.to_thread(collect_system_metrics)
            db_health = await asyncio.to_thread(check_database_health)
            ai_health = await self.probe_ai(actual_client, force=force_ai_probe)
            api_health = await asyncio.to_thread(get_api_health)
            timestamp = datetime.now(UTC).isoformat()
            snapshot = {
                "collected_at": timestamp,
                "system": system,
                "thresholds": database.get_monitor_settings(DEFAULT_ALERT_SETTINGS),
                "backend": {
                    "status": "healthy",
                    "uptime_seconds": system["application_uptime_seconds"],
                },
                "database": db_health,
                "groq": ai_health,
                "api": api_health,
                "websocket": {
                    "status": "connected" if websocket_connections else "disconnected",
                    "connections": websocket_connections,
                },
                "agents": await asyncio.to_thread(get_agent_health),
            }
            return snapshot

    async def sample_once(self, client, websocket_connections: int) -> dict[str, Any]:
        actual_client = client() if callable(client) else client
        snapshot = await self.collect(actual_client, websocket_connections)
        database.record_monitor_sample(snapshot)
        await evaluate_alerts(snapshot)
        return snapshot


async def evaluate_alerts(snapshot: dict[str, Any]) -> None:
    settings = database.get_monitor_settings(DEFAULT_ALERT_SETTINGS)
    findings: list[tuple[str, str, str, str]] = []
    system = snapshot["system"]

    for metric, label in (
        ("cpu_percent", "CPU utilization"),
        ("memory_percent", "Memory utilization"),
        ("disk_percent", "Disk utilization"),
    ):
        value = system[metric]
        threshold = settings[metric]
        key = f"high_{metric}"
        if value >= threshold:
            findings.append(
                (key, "warning", f"High {label.lower()}", f"Measured at {value}% (configured threshold: {threshold}%).")
            )
        else:
            database.resolve_alert(key)

    if snapshot["database"]["status"] != "healthy":
        findings.append(
            ("database_unhealthy", "critical", "Database health check failed", snapshot["database"].get("error") or snapshot["database"].get("check", "Database check failed."))
        )
    else:
        database.resolve_alert("database_unhealthy")

    if snapshot["groq"]["status"] == "unavailable":
        findings.append(
            ("groq_unavailable", "warning", "Groq API unavailable", snapshot["groq"].get("error") or "The provider health check failed.")
        )
    else:
        database.resolve_alert("groq_unavailable")

    api = snapshot["api"]
    if api["requests"] and api["average_latency_ms"] >= settings["api_latency_ms"]:
        findings.append(
            (
                "high_api_latency",
                "warning",
                "API response time is elevated",
                f"Five-minute average is {api['average_latency_ms']} ms (configured threshold: {settings['api_latency_ms']} ms).",
            )
        )
    else:
        database.resolve_alert("high_api_latency")

    disconnects = database.count_websocket_disconnects(
        (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    )
    if disconnects >= settings["websocket_disconnects"]:
        findings.append(
            (
                "websocket_disconnects",
                "warning",
                "Repeated WebSocket disconnections",
                f"{disconnects} disconnections were recorded in the last five minutes.",
            )
        )
    else:
        database.resolve_alert("websocket_disconnects")

    recent_runs = database.list_agent_runs(
        (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    )
    failing_agents = {
        run["agent"] for run in recent_runs if run["status"] == "failed"
    }
    for agent in ("planner", "researcher", "executor", "verifier", "communicator"):
        alert_key = f"agent_failure:{agent}"
        if agent in failing_agents:
            findings.append(
                (
                    alert_key,
                    "warning",
                    f"{agent.title()} execution failed",
                    "An agent execution failed in the last five minutes. Review the error history for details.",
                )
            )
        else:
            database.resolve_alert(alert_key)

    for alert_key, severity, title, message in findings:
        alert, created = database.create_or_update_alert(
            alert_key, severity, title, message
        )
        if created:
            await publish_alert(alert)


async def publish_alert(alert: dict[str, Any]) -> None:
    try:
        from app.main import manager

        await manager.broadcast({"type": "monitor_alert", "alert": alert})
    except RuntimeError:
        logger.exception("Could not publish monitoring alert")


async def monitor_loop(
    client, get_websocket_connections, stop_event: asyncio.Event
) -> None:
    while not stop_event.is_set():
        actual_client = client() if callable(client) else client
        try:
            snapshot = await monitor_service.sample_once(
                actual_client, get_websocket_connections()
            )
            await publish_sample(snapshot)
        except Exception:
            logger.exception("Monitoring sample collection failed")
            try:
                database.record_application_error(
                    "monitoring", "Monitoring sample collection failed; see server logs."
                )
            except Exception:
                logger.exception("Could not persist monitoring collection failure")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=15)
        except TimeoutError:
            continue


async def publish_sample(snapshot: dict[str, Any]) -> None:
    try:
        from app.main import manager

        await manager.broadcast({"type": "monitor_update", "snapshot": snapshot})
    except RuntimeError:
        logger.exception("Could not publish monitoring sample")


monitor_service = MonitorService()
