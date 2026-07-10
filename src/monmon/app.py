"""Textual TUI for monmon."""

from __future__ import annotations

import queue
import time
from collections import deque
from collections.abc import Iterable

import psutil
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Static
from textual.widgets.data_table import CellDoesNotExist

from .power import (
    ClusterSample,
    PowermetricsError,
    PowermetricsReader,
    PowerSample,
)
from .procs import snapshot as proc_snapshot

BAR_CHARS = " ▏▎▍▌▋▊▉█"
SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _load_color(ratio: float) -> str:
    if ratio < 0.33:
        return "green"
    if ratio < 0.66:
        return "yellow"
    return "red"


def spark(values: Iterable[float], width: int = 24, style: str | None = None) -> Text:
    """Render the most recent `width` values (each 0..1) as a sparkline.

    Colored per-value by load unless a fixed `style` is given.
    """
    vals = list(values)[-width:]
    text = Text(" " * (width - len(vals)))
    for v in vals:
        v = max(0.0, min(1.0, v))
        ch = SPARK_CHARS[round(v * (len(SPARK_CHARS) - 1))]
        text.append(ch, style=style or _load_color(v))
    return text


class History:
    """Rolling per-metric samples feeding the sparklines (most recent last)."""

    def __init__(self, maxlen: int = 60) -> None:
        self.cpu_active: deque[float] = deque(maxlen=maxlen)
        self.gpu_active: deque[float] = deque(maxlen=maxlen)
        self.ane_ratio: deque[float] = deque(maxlen=maxlen)
        self.package_mw: deque[float] = deque(maxlen=maxlen)
        self.ram_used: deque[float] = deque(maxlen=maxlen)


def overall_cpu_active(sample: PowerSample) -> float:
    """Mean active-residency across all cores (clusters as fallback)."""
    cores = [core for cluster in sample.clusters for core in cluster.cores]
    if cores:
        return sum(c.active for c in cores) / len(cores)
    if sample.clusters:
        return sum(c.active for c in sample.clusters) / len(sample.clusters)
    return 0.0


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
    return Text(bar, style=_load_color(ratio))


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

    def __init__(self, history: History, id: str | None = None) -> None:
        super().__init__(id=id)
        self.history = history

    def render(self):
        if self.sample is None:
            return Panel(Text("waiting for samples…", style="dim"), title="CPU", border_style="blue")

        groups: list = []
        for cluster in self.sample.clusters:
            groups.append(
                Text.from_markup(
                    f"  {cluster_label(cluster)} · avg {cluster.active * 100:5.1f}%"
                    f"  @ {fmt_mhz(cluster.freq_mhz)}",
                    overflow="ellipsis",
                )
            )
            t = Table.grid(padding=(0, 1))
            t.add_column(justify="right", style="dim", min_width=6)
            t.add_column(min_width=22)
            t.add_column(justify="right", min_width=8)
            t.add_column(justify="right", style="dim", min_width=9)
            for core in cluster.cores:
                t.add_row(
                    f"cpu{core.cpu_id}",
                    hbar(core.active, 24),
                    f"{core.active * 100:5.1f}%",
                    fmt_mhz(core.freq_mhz),
                )
            groups.append(t)
            groups.append(Text(""))

        if self.history.cpu_active:
            t = Table.grid(padding=(0, 1))
            t.add_column(justify="right", style="dim", min_width=6)
            t.add_column(min_width=22)
            t.add_column(justify="right", min_width=8)
            t.add_row(
                "hist",
                spark(self.history.cpu_active, 24),
                f"{self.history.cpu_active[-1] * 100:5.1f}%",
            )
            groups.append(t)

        title = f"CPU · {fmt_mw(self.sample.cpu_power_mw)}"
        return Panel(Group(*groups), title=title, border_style="blue")


class GpuPanel(Static):
    sample: reactive[PowerSample | None] = reactive(None)

    def __init__(self, history: History, id: str | None = None) -> None:
        super().__init__(id=id)
        self.history = history

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
        t.add_row("hist", spark(self.history.gpu_active, 28), "")
        return Panel(t, title="GPU", border_style="green")


class AnePanel(Static):
    sample: reactive[PowerSample | None] = reactive(None)
    ceiling_mw: reactive[float] = reactive(8000.0)

    def __init__(self, history: History, id: str | None = None) -> None:
        super().__init__(id=id)
        self.history = history

    def render(self):
        if self.sample is None:
            return Panel(Text("waiting…", style="dim"), title="NPU (ANE)", border_style="magenta")
        s = self.sample
        # ANE has no active-residency counter exposed by powermetrics, so we
        # approximate "in use" from power draw. The ceiling starts at a
        # conservative 8 W and self-calibrates up to the session's peak draw.
        mw = s.ane_power_mw or 0.0
        ratio = min(1.0, mw / self.ceiling_mw) if self.ceiling_mw else 0.0
        t = Table.grid(padding=(0, 1))
        t.add_column(justify="right", style="dim", min_width=10)
        t.add_column(min_width=24)
        t.add_column(justify="right", min_width=8)
        in_use = mw > 1.0
        status = Text("● in use", style="bold magenta") if in_use else Text("○ idle", style="dim")
        t.add_row("state", status, "")
        t.add_row("power", hbar(ratio, 28), fmt_mw(s.ane_power_mw))
        t.add_row("hist", spark(self.history.ane_ratio, 28), "")
        return Panel(t, title="NPU (Apple Neural Engine)", border_style="magenta")


class SummaryBar(Static):
    sample: reactive[PowerSample | None] = reactive(None)
    stream_alive: reactive[bool] = reactive(True)
    paused: reactive[bool] = reactive(False)

    def __init__(self, history: History, id: str | None = None) -> None:
        super().__init__(id=id)
        self.history = history

    def render(self):
        left = Text()
        if self.sample and self.sample.hw_model:
            left.append(f"{self.sample.hw_model}  ", style="bold")
        left.append(f"{psutil.cpu_count(logical=False) or '?'} cores physical / ")
        left.append(f"{psutil.cpu_count(logical=True) or '?'} logical   ")

        if self.sample:
            left.append(f"package: {fmt_mw(self.sample.package_power_mw)}", style="yellow")
            peak = max(self.history.package_mw, default=0.0)
            if peak > 0:
                left.append("  ")
                left.append_text(spark((v / peak for v in self.history.package_mw), 20, style="yellow"))
        if self.paused:
            left.append("  ⏸ paused", style="bold yellow")
        if not self.stream_alive:
            left.append("  powermetrics ended — quit and rerun: sudo -v && monmon", style="bold red")
        return left


class MemPanel(Static):
    mem: reactive[tuple | None] = reactive(None)

    def __init__(self, history: History, id: str | None = None) -> None:
        super().__init__(id=id)
        self.history = history

    def render(self):
        if self.mem is None:
            return Panel(Text("waiting…", style="dim"), title="Memory", border_style="yellow")
        vm, swap = self.mem
        gib = 1024**3
        used_gb = (vm.total - vm.available) / gib
        t = Table.grid(padding=(0, 1))
        t.add_column(justify="right", style="dim", min_width=10)
        t.add_column(min_width=24)
        t.add_column(justify="right", min_width=8)
        t.add_row("ram", hbar(vm.percent / 100.0, 28), f"{vm.percent:5.1f}%")
        t.add_row("", Text(f"{used_gb:.1f} / {vm.total / gib:.1f} GB used", style="dim"), "")
        if swap.total:
            t.add_row(
                "swap",
                hbar(swap.percent / 100.0, 28),
                f"{swap.used / gib:5.1f} GB",
            )
        else:
            t.add_row("swap", Text("none", style="dim"), "")
        t.add_row("hist", spark(self.history.ram_used, 28), "")
        return Panel(t, title="Memory", border_style="yellow")


HELP_TEXT = """\
[bold]monmon keys[/bold]

  [bold cyan]s[/]  sort process table by cpu / mem
  [bold cyan]/[/]  filter processes by name (esc clears)
  [bold cyan]k[/]  kill the selected process (asks first)
  [bold cyan]p[/]  pause / resume sampling
  [bold cyan]?[/]  this help
  [bold cyan]q[/]  quit

[dim]Data: powermetrics (CPU / GPU / ANE) + psutil (processes, memory).
The ANE bar scales to an 8 W ceiling that self-calibrates to the
session's peak draw.[/dim]"""


class HelpScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close", show=False),
        Binding("question_mark", "close", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static(HELP_TEXT, id="help-body")

    def action_close(self) -> None:
        self.app.pop_screen()


class ConfirmKillScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n,escape", "cancel", "No"),
    ]

    def __init__(self, pid: int, proc_name: str) -> None:
        super().__init__()
        self.pid = pid
        self.proc_name = proc_name

    def compose(self) -> ComposeResult:
        yield Static(
            f"Kill [bold]{self.proc_name}[/] (pid {self.pid})?   [bold green]y[/] / [bold red]n[/]",
            id="confirm-body",
        )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ProcPanel(Static):
    class FilterChanged(Message):
        pass

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        self.sort_by = "cpu"  # or "mem"
        self.filter_text = ""

    def compose(self) -> ComposeResult:
        yield Input(placeholder="filter processes… (esc clears)", id="proc-filter")
        yield DataTable(id="proc-table", zebra_stripes=True, cursor_type="row")

    def show_filter(self) -> None:
        inp = self.query_one("#proc-filter", Input)
        inp.display = True
        inp.focus()

    def hide_filter(self) -> None:
        inp = self.query_one("#proc-filter", Input)
        inp.value = ""
        inp.display = False
        self.filter_text = ""
        self.query_one("#proc-table", DataTable).focus()
        self.post_message(self.FilterChanged())

    def on_input_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value
        self.post_message(self.FilterChanged())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#proc-table", DataTable).focus()

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape" and self.query_one("#proc-filter", Input).has_focus:
            event.stop()
            self.hide_filter()

    def on_mount(self) -> None:
        table: DataTable = self.query_one("#proc-table", DataTable)
        cols = table.add_columns("PID", "Process", "CPU %", "Mem MB")
        self._name_col, self._cpu_col, self._mem_col = cols[1:]

    def update_rows(self, rows) -> None:
        """Diff rows into the table keyed by PID so cursor and scroll survive."""
        table: DataTable = self.query_one("#proc-table", DataTable)

        cursor_key = None
        if table.row_count:
            try:
                cursor_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            except CellDoesNotExist:
                pass

        alive = {str(r.pid) for r in rows}
        for gone in [k for k in table.rows if k.value not in alive]:
            table.remove_row(gone)

        for r in rows:
            key = str(r.pid)
            name = r.name[:32]
            cpu = f"{r.cpu_percent:6.1f}"
            mem = f"{r.mem_mb:8.1f}"
            if key in table.rows:
                table.update_cell(key, self._name_col, name, update_width=True)
                table.update_cell(key, self._cpu_col, cpu)
                table.update_cell(key, self._mem_col, mem)
            else:
                table.add_row(key, name, cpu, mem, key=key)

        sort_col = self._mem_col if self.sort_by == "mem" else self._cpu_col
        table.sort(sort_col, key=float, reverse=True)

        if cursor_key is not None and cursor_key.value in alive:
            table.move_cursor(row=table.get_row_index(cursor_key))


class MonMonApp(App):
    CSS = """
    Screen { layout: vertical; }
    #top-row { height: 1fr; }
    #left { width: 2fr; }
    #right { width: 1fr; }
    CpuPanel { height: 1fr; }
    GpuPanel { height: auto; min-height: 8; }
    AnePanel { height: auto; min-height: 8; }
    MemPanel { height: auto; min-height: 8; }
    ProcPanel { height: 1fr; }
    SummaryBar { height: 1; padding: 0 1; background: $panel; }
    #proc-filter { display: none; height: 3; }
    HelpScreen, ConfirmKillScreen { align: center middle; }
    #help-body, #confirm-body {
        width: auto; max-width: 72; height: auto;
        padding: 1 2; background: $surface; border: round $primary;
    }
    Screen.-narrow #top-row { layout: vertical; }
    Screen.-narrow #left { width: 1fr; height: 45%; }
    Screen.-narrow #right { width: 1fr; height: 1fr; }
    """
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (100, "-wide")]
    # Never auto-focus the hidden filter Input — letters would type into it
    # invisibly instead of reaching the app bindings.
    AUTO_FOCUS = "#proc-table"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("s", "toggle_sort", "Sort cpu/mem"),
        Binding("slash", "filter_procs", "Filter", key_display="/"),
        Binding("k", "kill_proc", "Kill"),
        Binding("p", "toggle_pause", "Pause"),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(self, interval_ms: int = 1000) -> None:
        super().__init__()
        self.interval_ms = interval_ms
        self.reader = PowermetricsReader(interval_ms=interval_ms)
        self.history = History()
        self._ane_ceiling_mw = 8000.0
        self._last_error = 0.0
        self._stream_dead = False
        self._paused = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield SummaryBar(self.history, id="summary")
        with Horizontal(id="top-row"):
            with Vertical(id="left"):
                yield CpuPanel(self.history, id="cpu")
            with Vertical(id="right"):
                yield GpuPanel(self.history, id="gpu")
                yield AnePanel(self.history, id="ane")
                yield MemPanel(self.history, id="mem")
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

        if not self._stream_dead and not self.reader.is_alive():
            self._stream_dead = True
            self.query_one("#summary", SummaryBar).stream_alive = False
            self.notify(
                "powermetrics stream ended — quit and rerun: sudo -v && monmon",
                severity="error",
                timeout=10,
            )

        if drained is None or self._paused:
            return
        ane_mw = drained.ane_power_mw or 0.0
        self._ane_ceiling_mw = max(self._ane_ceiling_mw, ane_mw)
        self.history.cpu_active.append(overall_cpu_active(drained))
        self.history.gpu_active.append(drained.gpu_active)
        self.history.ane_ratio.append(min(1.0, ane_mw / self._ane_ceiling_mw))
        self.history.package_mw.append(drained.package_power_mw or 0.0)

        self.query_one("#cpu", CpuPanel).sample = drained
        self.query_one("#gpu", GpuPanel).sample = drained
        ane = self.query_one("#ane", AnePanel)
        ane.ceiling_mw = self._ane_ceiling_mw
        ane.sample = drained
        self.query_one("#summary", SummaryBar).sample = drained

    def _poll_procs(self) -> None:
        if self._paused:
            return
        panel = self.query_one("#proc", ProcPanel)
        panel.update_rows(proc_snapshot(limit=18, by=panel.sort_by, contains=panel.filter_text))

        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        self.history.ram_used.append(vm.percent / 100.0)
        self.query_one("#mem", MemPanel).mem = (vm, swap)

    def on_proc_panel_filter_changed(self, message: ProcPanel.FilterChanged) -> None:
        self._poll_procs()

    def action_toggle_sort(self) -> None:
        panel = self.query_one("#proc", ProcPanel)
        panel.sort_by = "mem" if panel.sort_by == "cpu" else "cpu"
        self._poll_procs()

    def action_filter_procs(self) -> None:
        self.query_one("#proc", ProcPanel).show_filter()

    def action_toggle_pause(self) -> None:
        self._paused = not self._paused
        self.query_one("#summary", SummaryBar).paused = self._paused

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_kill_proc(self) -> None:
        table = self.query_one("#proc-table", DataTable)
        if not table.row_count:
            return
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except CellDoesNotExist:
            return
        pid = int(key.value or -1)
        name = str(table.get_row(key)[1])

        def _done(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                psutil.Process(pid).terminate()
                self.notify(f"sent SIGTERM to {name} ({pid})", timeout=4)
            except psutil.NoSuchProcess:
                self.notify(f"{name} ({pid}) already exited", severity="warning", timeout=4)
            except psutil.AccessDenied:
                self.notify(f"permission denied for {name} ({pid})", severity="error", timeout=4)
            self._poll_procs()

        self.push_screen(ConfirmKillScreen(pid, name), _done)

    def on_unmount(self) -> None:
        self.reader.stop()


def run(interval_ms: int = 1000) -> None:
    MonMonApp(interval_ms=interval_ms).run()
