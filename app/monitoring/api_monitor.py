from datetime import UTC, datetime, timedelta
from typing import Any

from app import database


def window_start(minutes: int = 5) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def get_api_health() -> dict[str, Any]:
    return database.get_api_metrics(window_start())


def record_request(route: str, status_code: int, latency_ms: float) -> None:
    database.record_api_metric(route, status_code, latency_ms)
