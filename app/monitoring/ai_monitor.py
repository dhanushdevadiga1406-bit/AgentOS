import time
from typing import Any

from groq import AsyncGroq


async def probe_groq(client: AsyncGroq | None) -> dict[str, Any]:
    if client is None:
        return {
            "status": "not_configured",
            "available": False,
            "latency_ms": None,
            "error": "GROQ_API_KEY is not configured.",
        }

    started = time.perf_counter()
    try:
        await client.models.list(timeout=8.0)
    except Exception as error:
        return {
            "status": "unavailable",
            "available": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": f"{type(error).__name__}: {str(error)[:300]}",
        }
    return {
        "status": "available",
        "available": True,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "error": None,
    }
