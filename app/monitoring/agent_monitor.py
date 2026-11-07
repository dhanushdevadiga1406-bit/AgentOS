from typing import Any

from app import database


def get_agent_health() -> list[dict[str, Any]]:
    return database.get_agent_statistics()
