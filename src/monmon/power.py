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
import subprocess
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field


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


def _freq_mhz(v: float | int | None) -> float:
    """Normalize a frequency reading to MHz.

    `freq_hz` holds Hz on most macOS versions, but some blocks report MHz in
    the same key (e.g. the GPU on macOS 27). No real clock falls between
    100 kHz-as-Hz and 100 GHz-as-MHz, so split on magnitude.
    """
    if not v:
        return 0.0
    v = float(v)
    return v if v < 1e5 else v / 1e6


def _active_ratio(entry: dict) -> float:
    """Active residency: time neither idle nor powered down.

    macOS 27 reports power-gated time as `down_ratio`, which `idle_ratio`
    does NOT include — a fully gated core reads idle=0/down=1 and would
    look 100% active if we only subtracted idle.
    """
    idle = entry.get("idle_ratio")
    down = entry.get("down_ratio")
    if idle is None and down is None:
        return 0.0
    try:
        total = float(idle or 0.0) + float(down or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, 1.0 - total))


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
                freq_mhz=_freq_mhz(cpu.get("freq_hz")),
                active=_active_ratio(cpu),
            )
            for cpu in (c.get("cpus") or [])
        ]
        # Cluster-level idle_ratio measures whole-cluster power-gating, which
        # reads ~0 under any load; the per-core mean matches the "avg" label.
        if cores:
            active = sum(core.active for core in cores) / len(cores)
        else:
            active = _active_ratio(c)
        clusters.append(
            ClusterSample(
                name=name,
                kind=_cluster_kind(name),
                freq_mhz=_freq_mhz(c.get("freq_hz")),
                active=active,
                cores=cores,
            )
        )

    gpu = doc.get("gpu", {}) or {}
    gpu_freq = _freq_mhz(gpu.get("freq_hz"))
    gpu_active = _active_ratio(gpu)

    elapsed_ns = int(doc.get("elapsed_ns", 0) or 0)

    # Keys vary across macOS versions: some report power in mW, others report
    # energy in mJ accumulated over the sample window.
    def _num(*keys: str) -> float | None:
        for src in (processor, doc):
            for k in keys:
                v = src.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
        return None

    def _mw(power_keys: tuple[str, ...], energy_keys: tuple[str, ...]) -> float | None:
        mw = _num(*power_keys)
        if mw is not None:
            return mw
        mj = _num(*energy_keys)
        if mj is not None and elapsed_ns > 0:
            return mj * 1e9 / elapsed_ns  # mJ over the window -> mW
        return None

    return PowerSample(
        elapsed_ns=elapsed_ns,
        clusters=clusters,
        gpu_freq_mhz=gpu_freq,
        gpu_active=gpu_active,
        gpu_power_mw=_mw(("gpu_power",), ("gpu_energy",)),
        ane_power_mw=_mw(("ane_power",), ("ane_energy",)),
        cpu_power_mw=_mw(("cpu_power",), ("cpu_energy",)),
        package_power_mw=_mw(
            ("package_power", "combined_power"),
            ("package_energy", "combined_energy"),
        ),
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
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name="powermetrics-stderr", daemon=True
        )
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

    def is_alive(self) -> bool:
        """True while the powermetrics subprocess is still running."""
        return self._proc is not None and self._proc.poll() is None

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
            # permitted, so we shell out through sudo to send the signal. If
            # sudo can't kill without a password (e.g. a NOPASSWD rule scoped
            # to powermetrics only), fail quietly — powermetrics exits on
            # SIGPIPE once our end of the pipe closes at process exit.
            try:
                r = subprocess.run(
                    ["sudo", "-n", "kill", "-TERM", str(self._proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=2,
                )
            except Exception:  # noqa: BLE001
                return
            if r.returncode != 0:
                return
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    subprocess.run(
                        ["sudo", "-n", "kill", "-KILL", str(self._proc.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=2,
                    )
                except Exception:  # noqa: BLE001
                    pass


def can_run_powermetrics() -> bool:
    """True if `sudo -n powermetrics` will start without a password prompt.

    Satisfied by cached sudo credentials or by a NOPASSWD sudoers rule
    scoped to powermetrics (see README) — `sudo -l <cmd>` checks both.
    """
    pm = shutil.which("powermetrics")
    if pm is None:
        return False
    try:
        r = subprocess.run(
            ["sudo", "-n", "-l", pm],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False
