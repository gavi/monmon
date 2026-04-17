"""Textual TUI for monmon."""

from __future__ import annotations

import queue
import time

import psutil
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Static

from .power import (
    ClusterSample,
    PowerSample,
    PowermetricsError,
    PowermetricsReader,
    ensure_sudo_cached,
)
from .procs import snapshot as proc_snapshot


BAR_CHARS = " ▏▎▍▌▋▊▉█"


def hbar(ratio: float, width: int = 20) -> Text:
    ratio = max(0.0, min(1.0, ratio))
    total = ratio * width
    full = int(total)
    remainder = total - full
    partial_idx = int(remainder * (len(BAR_CHARS) - 1))
    bar = "█" * full
    if full < width:
        bar += BAR_CHARS[partial_idx]
        bar += " " * (width - full - 1)
    # color by load
    if ratio < 0.33:
        color = "green"
    elif ratio < 0.66:
        color = "yellow"
    else:
        color = "red"
    return Text(bar, style=color)


def fmt_mhz(mhz: float) -> str:
    if mhz >= 1000:
        return f"{mhz / 1000:.2f} GHz"
    return f"{mhz:.0f} MHz"


def fmt_mw(mw: float | None) -> str:
    if mw is None:
        return "—"
    if mw >= 1000:
        return f"{mw / 1000:.2f} W"
    return f"{mw:.0f} mW"


def cluster_label(c: ClusterSample) -> str:
    if c.kind == "E":
        return f"[bold cyan]{c.name}[/] (efficiency)"
    if c.kind == "P":
        return f"[bold magenta]{c.name}[/] (performance)"
    return f"[bold]{c.name}[/]"


class CpuPanel(Static):
    sample: reactive[PowerSample | None] = reactive(None)

    def render(self):
        if self.sample is None:
            return Panel(Text("waiting for samples…", style="dim"), title="CPU", border_style="blue")

        groups: list = []
        for cluster in self.sample.clusters:
            t = Table.grid(padding=(0, 1))
            t.add_column(justify="right", style="dim", min_width=6)
            t.add_column(min_width=22)
            t.add_column(justify="right", min_width=8)
            t.add_column(justify="right", style="dim", min_width=9)

            header = Text.from_markup(
                f"{cluster_label(cluster)} · avg {cluster.active * 100:5.1f}%  @ {fmt_mhz(cluster.freq_mhz)}"
            )
            t.add_row("", header, "", "")
            for core in cluster.cores:
                t.add_row(
                    f"cpu{core.cpu_id}",
                    hbar(core.active, 24),
                    f"{core.active * 100:5.1f}%",
                    fmt_mhz(core.freq_mhz),
                )
            groups.append(t)
            groups.append(Text(""))

        title = f"CPU · {fmt_mw(self.sample.cpu_power_mw)}"
        return Panel(Group(*groups), title=title, border_style="blue")


class GpuPanel(Static):
    sample: reactive[PowerSample | None] = reactive(None)

    def render(self):
        if self.sample is None:
            return Panel(Text("waiting…", style="dim"), title="GPU", border_style="green")
        s = self.sample
        t = Table.grid(padding=(0, 1))
        t.add_column(justify="right", style="dim", min_width=10)
        t.add_column(min_width=24)
        t.add_column(justify="right", min_width=8)
        t.add_row("active", hbar(s.gpu_active, 28), f"{s.gpu_active * 100:5.1f}%")
        t.add_row("freq", Text(fmt_mhz(s.gpu_freq_mhz)), "")
        t.add_row("power", Text(fmt_mw(s.gpu_power_mw)), "")
        return Panel(t, title="GPU", border_style="green")


class AnePanel(Static):
    sample: reactive[PowerSample | None] = reactive(None)

    def render(self):
        if self.sample is None:
            return Panel(Text("waiting…", style="dim"), title="NPU (ANE)", border_style="magenta")
        s = self.sample
        # ANE has no active-residency counter exposed by powermetrics, so we
        # approximate "in use" from power draw. Peak varies by chip; use a
        # conservative 8 W ceiling so the bar reacts without maxing out.
        mw = s.ane_power_mw or 0.0
        ratio = min(1.0, mw / 8000.0)
        t = Table.grid(padding=(0, 1))
        t.add_column(justify="right", style="dim", min_width=10)
        t.add_column(min_width=24)
        t.add_column(justify="right", min_width=8)
        in_use = mw > 1.0
        status = Text("● in use", style="bold magenta") if in_use else Text("○ idle", style="dim")
        t.add_row("state", status, "")
        t.add_row("power", hbar(ratio, 28), fmt_mw(s.ane_power_mw))
        return Panel(t, title="NPU (Apple Neural Engine)", border_style="magenta")


class SummaryBar(Static):
    sample: reactive[PowerSample | None] = reactive(None)
    sudo_ok: reactive[bool] = reactive(True)

    def render(self):
        left = Text()
        if self.sample and self.sample.hw_model:
            left.append(f"{self.sample.hw_model}  ", style="bold")
        left.append(f"{psutil.cpu_count(logical=False) or '?'} cores physical / ")
        left.append(f"{psutil.cpu_count(logical=True) or '?'} logical   ")

        if self.sample:
            left.append(f"package: {fmt_mw(self.sample.package_power_mw)}", style="yellow")
        if not self.sudo_ok:
            left.append("  sudo credentials expired — restart", style="bold red")
        return left


class ProcPanel(Static):
    def compose(self) -> ComposeResult:
        yield DataTable(id="proc-table", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        table: DataTable = self.query_one("#proc-table", DataTable)
        table.add_columns("PID", "Process", "CPU %", "Mem MB")

    def update_rows(self, rows) -> None:
        table: DataTable = self.query_one("#proc-table", DataTable)
        table.clear()
        for r in rows:
            table.add_row(
                str(r.pid),
                r.name[:32],
                f"{r.cpu_percent:6.1f}",
                f"{r.mem_mb:8.1f}",
            )


class MonMonApp(App):
    CSS = """
    Screen { layout: vertical; }
    #top-row { height: 1fr; }
    #left { width: 2fr; }
    #right { width: 1fr; }
    CpuPanel { height: 1fr; }
    GpuPanel { height: auto; min-height: 8; }
    AnePanel { height: auto; min-height: 8; }
    ProcPanel { height: 1fr; }
    SummaryBar { height: 1; padding: 0 1; background: $panel; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(self, interval_ms: int = 1000) -> None:
        super().__init__()
        self.interval_ms = interval_ms
        self.reader = PowermetricsReader(interval_ms=interval_ms)
        self._last_error = 0.0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield SummaryBar(id="summary")
        with Horizontal(id="top-row"):
            with Vertical(id="left"):
                yield CpuPanel(id="cpu")
            with Vertical(id="right"):
                yield GpuPanel(id="gpu")
                yield AnePanel(id="ane")
                yield ProcPanel(id="proc")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "monmon"
        self.sub_title = "E/P cores · GPU · NPU"
        try:
            self.reader.start()
        except PowermetricsError as exc:
            self.notify(f"Cannot start powermetrics: {exc}", severity="error", timeout=10)
            return
        self.set_interval(max(0.2, self.interval_ms / 1000.0), self._poll_power)
        self.set_interval(1.5, self._poll_procs)

    def _poll_power(self) -> None:
        drained = None
        try:
            while True:
                drained = self.reader.samples.get_nowait()
        except queue.Empty:
            pass

        # Surface any stderr noise, but rate-limit so we don't spam.
        now = time.monotonic()
        if now - self._last_error > 5.0:
            try:
                msg = self.reader.errors.get_nowait()
                self.notify(msg, severity="warning", timeout=4)
                self._last_error = now
            except queue.Empty:
                pass

        if drained is None:
            return
        self.query_one("#cpu", CpuPanel).sample = drained
        self.query_one("#gpu", GpuPanel).sample = drained
        self.query_one("#ane", AnePanel).sample = drained
        summary = self.query_one("#summary", SummaryBar)
        summary.sample = drained
        summary.sudo_ok = ensure_sudo_cached()

    def _poll_procs(self) -> None:
        rows = proc_snapshot(limit=18)
        self.query_one("#proc", ProcPanel).update_rows(rows)

    def on_unmount(self) -> None:
        self.reader.stop()


def run(interval_ms: int = 1000) -> None:
    MonMonApp(interval_ms=interval_ms).run()
