"""Process snapshot tests against the live system via psutil."""

from __future__ import annotations

from monmon.procs import snapshot


def test_snapshot_sorts_by_requested_key() -> None:
    snapshot()  # first call primes psutil's cpu_percent deltas

    rows = snapshot(limit=10, by="mem")
    assert rows
    mems = [r.mem_mb for r in rows]
    assert mems == sorted(mems, reverse=True)

    rows = snapshot(limit=10, by="cpu")
    cpus = [r.cpu_percent for r in rows]
    assert cpus == sorted(cpus, reverse=True)
