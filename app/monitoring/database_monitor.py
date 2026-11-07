from typing import Any

from app import database


def check_database_health() -> dict[str, Any]:
    try:
        with database.connect() as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()[0]
            database_bytes = connection.execute(
                "SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()"
            ).fetchone()[0]
        healthy = result == "ok"
        return {
            "status": "healthy" if healthy else "degraded",
            "check": result,
            "size_bytes": database_bytes,
        }
    except Exception as error:
        return {"status": "unavailable", "error": str(error)[:500]}
