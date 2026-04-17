"""Per-process snapshots via psutil.

We deliberately keep this separate from `power` — powermetrics' task sampler
is brittle across macOS versions, and psutil gives us reliable CPU/memory
numbers that answer the "what is running?" question.
"""

from __future__ import annotations

from dataclasses import dataclass

import psutil


@dataclass
class ProcInfo:
    pid: int
    name: str
    cpu_percent: float  # may exceed 100 on multi-core
    mem_mb: float


_primed = False


def _prime() -> None:
    """psutil.cpu_percent needs a first call to seed its delta timer."""
    global _primed
    for p in psutil.process_iter(["pid"]):
        try:
            p.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _primed = True


def snapshot(limit: int = 20) -> list[ProcInfo]:
    if not _primed:
        _prime()
        return []

    rows: list[ProcInfo] = []
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            cpu = p.cpu_percent(None)
            mem = p.info["memory_info"]
            rows.append(
                ProcInfo(
                    pid=p.info["pid"],
                    name=p.info["name"] or "?",
                    cpu_percent=cpu,
                    mem_mb=(mem.rss / (1024 * 1024)) if mem else 0.0,
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    rows.sort(key=lambda r: r.cpu_percent, reverse=True)
    return rows[:limit]
