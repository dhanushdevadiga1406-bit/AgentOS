import os
import time
from typing import Any

import psutil


_started_at = time.monotonic()


def collect_system_metrics() -> dict[str, Any]:
    cpu_percent = psutil.cpu_percent(interval=0.1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(os.path.abspath(os.sep))
    return {
        "cpu_percent": round(cpu_percent, 2),
        "memory_percent": round(memory.percent, 2),
        "memory_used_bytes": memory.used,
        "memory_total_bytes": memory.total,
        "disk_percent": round(disk.percent, 2),
        "disk_used_bytes": disk.used,
        "disk_total_bytes": disk.total,
        "application_uptime_seconds": round(time.monotonic() - _started_at, 2),
    }


def application_uptime_seconds() -> float:
    return round(time.monotonic() - _started_at, 2)
