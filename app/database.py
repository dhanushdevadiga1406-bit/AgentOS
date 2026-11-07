import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _database_path() -> Path:
    configured_path = os.getenv("AGENTOS_DATABASE_PATH", "data/agentos.db")
    path = Path(configured_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


def connect() -> sqlite3.Connection:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, factory=_ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def now() -> str:
    return datetime.now(UTC).isoformat()


def initialize_database() -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                agent TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                requires_approval INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
                agent TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS monitoring_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                metrics TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS api_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                route TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                latency_ms REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
                agent TEXT NOT NULL,
                task TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_ms REAL,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS application_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                message TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS system_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_key TEXT NOT NULL,
                severity TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS diagnostic_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                report TEXT NOT NULL,
                snapshot TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS monitor_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS websocket_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages(conversation_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_conversation
                ON tasks(conversation_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_activity_created
                ON agent_activity(created_at);
            CREATE INDEX IF NOT EXISTS idx_monitoring_samples_created
                ON monitoring_samples(created_at);
            CREATE INDEX IF NOT EXISTS idx_api_metrics_created
                ON api_metrics(created_at);
            CREATE INDEX IF NOT EXISTS idx_agent_runs_created
                ON agent_runs(started_at);
            CREATE INDEX IF NOT EXISTS idx_application_errors_created
                ON application_errors(created_at);
            CREATE INDEX IF NOT EXISTS idx_system_alerts_status
                ON system_alerts(status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_websocket_events_created
                ON websocket_events(created_at);
            """
        )


def create_conversation(title: str) -> dict[str, Any]:
    timestamp = now()
    with connect() as connection:
        cursor = connection.execute(
            "INSERT INTO conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
            (title, timestamp, timestamp),
        )
        row = connection.execute(
            "SELECT * FROM conversations WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row)


def get_conversation(conversation_id: int) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        return dict(row) if row else None


def list_conversations() -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT c.*, (
                SELECT content FROM messages
                WHERE conversation_id = c.id ORDER BY id DESC LIMIT 1
            ) AS preview
            FROM conversations c ORDER BY c.updated_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def add_message(conversation_id: int, role: str, content: str) -> dict[str, Any]:
    timestamp = now()
    with connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO messages (conversation_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (conversation_id, role, content, timestamp),
        )
        connection.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (timestamp, conversation_id),
        )
        row = connection.execute(
            "SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row)


def list_messages(conversation_id: int) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def create_tasks(conversation_id: int, stages: list[tuple[str, str]]) -> list[int]:
    timestamp = now()
    with connect() as connection:
        cursors = []
        for agent, title in stages:
            cursor = connection.execute(
                """
                INSERT INTO tasks
                    (conversation_id, title, agent, status, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (conversation_id, title, agent, timestamp, timestamp),
            )
            cursors.append(cursor.lastrowid)
        return cursors


def update_task(
    task_id: int, status: str, detail: str = "", requires_approval: bool = False
) -> None:
    with connect() as connection:
        connection.execute(
            """
            UPDATE tasks SET status = ?, detail = ?, requires_approval = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, detail, int(requires_approval), now(), task_id),
        )


def list_tasks(conversation_id: int | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM tasks"
    parameters: tuple[Any, ...] = ()
    if conversation_id is not None:
        query += " WHERE conversation_id = ?"
        parameters = (conversation_id,)
    query += " ORDER BY created_at DESC, id"
    with connect() as connection:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def get_task(task_id: int) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None


def record_activity(
    conversation_id: int | None, agent: str, action: str, status: str
) -> None:
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO agent_activity (conversation_id, agent, action, status, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (conversation_id, agent, action, status, now()),
        )


def list_activity(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM agent_activity ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def delete_conversation(conversation_id: int) -> bool:
    with connect() as connection:
        cursor = connection.execute(
            "DELETE FROM conversations WHERE id = ?", (conversation_id,)
        )
        return cursor.rowcount > 0


def clear_history() -> None:
    with connect() as connection:
        connection.execute("DELETE FROM conversations")
        connection.execute("DELETE FROM agent_activity")


def get_preferences() -> dict[str, Any]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT key, value FROM user_preferences ORDER BY key"
        ).fetchall()
        return {row["key"]: json.loads(row["value"]) for row in rows}


def save_preference(key: str, value: Any) -> None:
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO user_preferences (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, json.dumps(value), now()),
        )


def record_monitor_sample(metrics: dict[str, Any]) -> None:
    with connect() as connection:
        connection.execute(
            "INSERT INTO monitoring_samples (created_at, metrics) VALUES (?, ?)",
            (now(), json.dumps(metrics)),
        )


def list_monitor_samples(since: str, limit: int = 500) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT created_at, metrics FROM monitoring_samples
            WHERE created_at >= ? ORDER BY id
            """,
            (since,),
        ).fetchall()
        if len(rows) > limit:
            if limit == 1:
                rows = [rows[-1]]
            else:
                rows = [
                    rows[round(index * (len(rows) - 1) / (limit - 1))]
                    for index in range(limit)
                ]
        return [
            {"created_at": row["created_at"], **json.loads(row["metrics"])}
            for row in rows
        ]


def record_api_metric(route: str, status_code: int, latency_ms: float) -> None:
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO api_metrics (created_at, route, status_code, latency_ms)
            VALUES (?, ?, ?, ?)
            """,
            (now(), route, status_code, round(latency_ms, 2)),
        )


def get_api_metrics(since: str) -> dict[str, Any]:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END), 0) AS errors,
                COALESCE(AVG(latency_ms), 0) AS average_latency_ms,
                COALESCE(MAX(latency_ms), 0) AS max_latency_ms
            FROM api_metrics WHERE created_at >= ?
            """,
            (since,),
        ).fetchone()
        return {
            "requests": row["total"],
            "errors": row["errors"],
            "error_rate_percent": round(row["errors"] * 100 / row["total"], 2)
            if row["total"]
            else 0.0,
            "average_latency_ms": round(row["average_latency_ms"], 2),
            "max_latency_ms": round(row["max_latency_ms"], 2),
        }


def start_agent_run(conversation_id: int, agent: str, task: str) -> int:
    with connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO agent_runs (conversation_id, agent, task, status, started_at)
            VALUES (?, ?, ?, 'running', ?)
            """,
            (conversation_id, agent, task, now()),
        )
        return int(cursor.lastrowid)


def finish_agent_run(run_id: int, status: str, duration_ms: float, error: str = "") -> None:
    with connect() as connection:
        connection.execute(
            """
            UPDATE agent_runs
            SET status = ?, finished_at = ?, duration_ms = ?, error = ?
            WHERE id = ?
            """,
            (status, now(), round(duration_ms, 2), error[:1000], run_id),
        )


def get_agent_statistics() -> list[dict[str, Any]]:
    agents = ("planner", "researcher", "executor", "verifier", "communicator")
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT agent,
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS successes,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failures,
                COALESCE(AVG(CASE WHEN status = 'completed' THEN duration_ms END), 0) AS average_duration_ms,
                (SELECT error FROM agent_runs recent
                    WHERE recent.agent = runs.agent AND recent.status = 'failed'
                    ORDER BY recent.id DESC LIMIT 1) AS last_error,
                (SELECT task FROM agent_runs current
                    WHERE current.agent = runs.agent AND current.status = 'running'
                    ORDER BY current.id DESC LIMIT 1) AS current_task
            FROM agent_runs runs
            GROUP BY agent
            """
        ).fetchall()
        by_agent = {row["agent"]: dict(row) for row in rows}
        result = []
        for agent in agents:
            row = by_agent.get(agent, {})
            successes = row.get("successes", 0) or 0
            failures = row.get("failures", 0) or 0
            total = row.get("total", 0) or 0
            result.append(
                {
                    "agent": agent,
                    "status": "running" if row.get("current_task") else "idle",
                    "current_task": row.get("current_task"),
                    "successes": successes,
                    "failures": failures,
                    "completion_rate_percent": round(successes * 100 / total, 2)
                    if total
                    else None,
                    "average_duration_ms": round(row.get("average_duration_ms", 0), 2)
                    if total
                    else None,
                    "last_error": row.get("last_error"),
                }
            )
        return result


def list_agent_runs(since: str, limit: int = 100) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM agent_runs WHERE started_at >= ?
            ORDER BY id DESC LIMIT ?
            """,
            (since, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def record_application_error(source: str, message: str) -> None:
    with connect() as connection:
        connection.execute(
            "INSERT INTO application_errors (created_at, source, message) VALUES (?, ?, ?)",
            (now(), source[:120], message[:2000]),
        )


def list_application_errors(since: str, limit: int = 30) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM application_errors WHERE created_at >= ?
            ORDER BY id DESC LIMIT ?
            """,
            (since, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def create_or_update_alert(
    alert_key: str, severity: str, title: str, message: str
) -> tuple[dict[str, Any], bool]:
    timestamp = now()
    with connect() as connection:
        row = connection.execute(
            """
            SELECT id, status FROM system_alerts
            WHERE alert_key = ? AND status IN ('active', 'acknowledged')
            ORDER BY id DESC LIMIT 1
            """,
            (alert_key,),
        ).fetchone()
        if row:
            connection.execute(
                """
                UPDATE system_alerts SET severity = ?, title = ?, message = ?, updated_at = ?
                WHERE id = ?
                """,
                (severity, title, message[:1000], timestamp, row["id"]),
            )
            alert_id = row["id"]
            created = False
        else:
            cursor = connection.execute(
                """
                INSERT INTO system_alerts
                    (alert_key, severity, title, message, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (alert_key, severity, title, message[:1000], timestamp, timestamp),
            )
            alert_id = cursor.lastrowid
            created = True
        alert = connection.execute(
            "SELECT * FROM system_alerts WHERE id = ?", (alert_id,)
        ).fetchone()
        return dict(alert), created


def resolve_alert(alert_key: str) -> None:
    with connect() as connection:
        connection.execute(
            """
            UPDATE system_alerts SET status = 'resolved', updated_at = ?
            WHERE alert_key = ? AND status IN ('active', 'acknowledged')
            """,
            (now(), alert_key),
        )


def list_alerts(active_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    query = "SELECT * FROM system_alerts"
    parameters: tuple[Any, ...] = ()
    if active_only:
        query += " WHERE status = 'active'"
    query += " ORDER BY id DESC LIMIT ?"
    parameters += (limit,)
    with connect() as connection:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def update_alert_status(alert_id: int, status: str) -> bool:
    with connect() as connection:
        cursor = connection.execute(
            """
            UPDATE system_alerts SET status = ?, updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (status, now(), alert_id),
        )
        return cursor.rowcount > 0


def save_monitor_setting(key: str, value: Any) -> None:
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO monitor_settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, json.dumps(value), now()),
        )


def get_monitor_settings(defaults: dict[str, Any]) -> dict[str, Any]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT key, value FROM monitor_settings"
        ).fetchall()
        return {**defaults, **{row["key"]: json.loads(row["value"]) for row in rows}}


def save_diagnostic_report(report: str, snapshot: dict[str, Any]) -> int:
    with connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO diagnostic_reports (created_at, report, snapshot)
            VALUES (?, ?, ?)
            """,
            (now(), report, json.dumps(snapshot)),
        )
        return int(cursor.lastrowid)


def list_diagnostic_reports(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT id, created_at, report FROM diagnostic_reports
            ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def record_websocket_event(event: str) -> None:
    with connect() as connection:
        connection.execute(
            "INSERT INTO websocket_events (event, created_at) VALUES (?, ?)",
            (event, now()),
        )


def count_websocket_disconnects(since: str) -> int:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS total FROM websocket_events
            WHERE event = 'disconnected' AND created_at >= ?
            """,
            (since,),
        ).fetchone()
        return int(row["total"])
