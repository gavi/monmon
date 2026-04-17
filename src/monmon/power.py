"""Stream samples from `powermetrics` as structured snapshots.

`powermetrics` is Apple's own telemetry CLI. With `-f plist` it emits one binary
plist per sample, separated by a NUL byte. We spawn it under `sudo` (it requires
root), read the stream, split on NUL, parse each plist, and expose a queue of
:class:`PowerSample` dataclasses that the TUI consumes.
"""

from __future__ import annotations

import plistlib
import queue
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class CoreSample:
    cpu_id: int
    freq_mhz: float
    active: float  # 0..1


@dataclass
class ClusterSample:
    name: str            # e.g. "E-Cluster", "P0-Cluster"
    kind: str            # "E" or "P"
    freq_mhz: float
    active: float        # 0..1
    cores: list[CoreSample] = field(default_factory=list)


@dataclass
class PowerSample:
    elapsed_ns: int
    clusters: list[ClusterSample]
    gpu_freq_mhz: float
    gpu_active: float           # 0..1
    gpu_power_mw: float | None  # may be absent on some chips
    ane_power_mw: float | None
    cpu_power_mw: float | None
    package_power_mw: float | None
    hw_model: str | None


class PowermetricsError(RuntimeError):
    pass


def _hz_to_mhz(v: float | int | None) -> float:
    if not v:
        return 0.0
    return float(v) / 1_000_000.0


def _active_from_idle(idle: float | int | None) -> float:
    if idle is None:
        return 0.0
    try:
        return max(0.0, min(1.0, 1.0 - float(idle)))
    except (TypeError, ValueError):
        return 0.0


def _cluster_kind(name: str) -> str:
    up = name.upper()
    if up.startswith("E"):
        return "E"
    if up.startswith("P"):
        return "P"
    return "?"


def parse_sample(plist_bytes: bytes) -> PowerSample:
    """Parse a single powermetrics plist sample into a PowerSample."""
    doc = plistlib.loads(plist_bytes)

    clusters: list[ClusterSample] = []
    processor = doc.get("processor", {}) or {}
    for c in processor.get("clusters", []) or []:
        name = c.get("name", "?")
        cores = [
            CoreSample(
                cpu_id=int(cpu.get("cpu", -1)),
                freq_mhz=_hz_to_mhz(cpu.get("freq_hz")),
                active=_active_from_idle(cpu.get("idle_ratio")),
            )
            for cpu in (c.get("cpus") or [])
        ]
        clusters.append(
            ClusterSample(
                name=name,
                kind=_cluster_kind(name),
                freq_mhz=_hz_to_mhz(c.get("freq_hz")),
                active=_active_from_idle(c.get("idle_ratio")),
                cores=cores,
            )
        )

    gpu = doc.get("gpu", {}) or {}
    gpu_freq = _hz_to_mhz(gpu.get("freq_hz"))
    gpu_active = _active_from_idle(gpu.get("idle_ratio"))

    # Power fields are in mW but keys vary across macOS versions.
    def _pw(*keys: str) -> float | None:
        for src in (processor, doc):
            for k in keys:
                v = src.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
        return None

    return PowerSample(
        elapsed_ns=int(doc.get("elapsed_ns", 0) or 0),
        clusters=clusters,
        gpu_freq_mhz=gpu_freq,
        gpu_active=gpu_active,
        gpu_power_mw=_pw("gpu_power"),
        ane_power_mw=_pw("ane_power", "ane_energy"),
        cpu_power_mw=_pw("cpu_power"),
        package_power_mw=_pw("package_power", "combined_power"),
        hw_model=doc.get("hw_model"),
    )


def _iter_plist_blocks(stream: Iterable[bytes]) -> Iterable[bytes]:
    """Split a byte stream into plist blocks separated by NUL bytes."""
    buf = bytearray()
    for chunk in stream:
        buf.extend(chunk)
        while True:
            idx = buf.find(b"\x00")
            if idx == -1:
                break
            block = bytes(buf[:idx])
            del buf[: idx + 1]
            if block.strip():
                yield block
    if buf.strip():
        yield bytes(buf)


class PowermetricsReader:
    """Background thread that streams PowerSample objects into a queue."""

    def __init__(self, interval_ms: int = 1000) -> None:
        if shutil.which("powermetrics") is None:
            raise PowermetricsError("powermetrics is not installed on this system")
        self.interval_ms = interval_ms
        self.samples: queue.Queue[PowerSample] = queue.Queue(maxsize=8)
        self.errors: queue.Queue[str] = queue.Queue(maxsize=32)
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        cmd = [
            "sudo",
            "-n",  # never prompt; caller must have cached credentials
            "powermetrics",
            "--samplers", "cpu_power,gpu_power,ane_power",
            "-i", str(self.interval_ms),
            "-f", "plist",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            raise PowermetricsError(str(exc)) from exc

        self._thread = threading.Thread(target=self._run, name="powermetrics-reader", daemon=True)
        self._thread.start()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, name="powermetrics-stderr", daemon=True)
        self._stderr_thread.start()

    def _run(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            def chunks() -> Iterable[bytes]:
                while not self._stop.is_set():
                    data = self._proc.stdout.read(65536)  # type: ignore[union-attr]
                    if not data:
                        return
                    yield data

            for block in _iter_plist_blocks(chunks()):
                try:
                    sample = parse_sample(block)
                except Exception as exc:  # noqa: BLE001
                    self.errors.put_nowait(f"parse error: {exc}")
                    continue
                # Drop oldest if consumer is slow — monitors are realtime.
                if self.samples.full():
                    try:
                        self.samples.get_nowait()
                    except queue.Empty:
                        pass
                self.samples.put_nowait(sample)
        finally:
            self._stop.set()

    def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for line in iter(self._proc.stderr.readline, b""):
            try:
                msg = line.decode("utf-8", "replace").rstrip()
            except Exception:  # noqa: BLE001
                continue
            if msg:
                try:
                    self.errors.put_nowait(msg)
                except queue.Full:
                    pass

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            # powermetrics runs as root via sudo; SIGTERM from our PID isn't
            # permitted, so we shell out through sudo to send the signal.
            try:
                subprocess.run(
                    ["sudo", "-n", "kill", "-TERM", str(self._proc.pid)],
                    check=False,
                    timeout=2,
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    subprocess.run(
                        ["sudo", "-n", "kill", "-KILL", str(self._proc.pid)],
                        check=False,
                        timeout=2,
                    )
                except Exception:  # noqa: BLE001
                    pass


def ensure_sudo_cached() -> bool:
    """Return True if `sudo -n` currently works without a password prompt."""
    try:
        r = subprocess.run(
            ["sudo", "-n", "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False
