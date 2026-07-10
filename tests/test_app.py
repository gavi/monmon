"""UI behavior tests via Textual's headless test harness."""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest
from rich.console import Console
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Input

from monmon.app import (
    AnePanel,
    ConfirmKillScreen,
    CpuPanel,
    HelpScreen,
    History,
    MemPanel,
    MonMonApp,
    ProcPanel,
    SummaryBar,
    fmt_mhz,
    fmt_mw,
    hbar,
    overall_cpu_active,
    spark,
)
from monmon.power import ClusterSample, CoreSample, PowerSample, can_run_powermetrics
from monmon.procs import ProcInfo


def make_sample(ane_mw: float = 500.0) -> PowerSample:
    return PowerSample(
        elapsed_ns=1_000_000_000,
        clusters=[
            ClusterSample(
                name="E-Cluster",
                kind="E",
                freq_mhz=1200.0,
                active=0.5,
                cores=[
                    CoreSample(cpu_id=0, freq_mhz=1200.0, active=0.25),
                    CoreSample(cpu_id=1, freq_mhz=1200.0, active=0.75),
                ],
            )
        ],
        gpu_freq_mhz=900.0,
        gpu_active=0.4,
        gpu_power_mw=400.0,
        ane_power_mw=ane_mw,
        cpu_power_mw=1500.0,
        package_power_mw=2500.0,
        hw_model="Mac14,10",
    )


def test_hbar_clamps_and_fills() -> None:
    assert hbar(1.5, width=20).plain == "█" * 20
    assert hbar(-0.2, width=20).plain == " " * 20
    assert len(hbar(0.5, width=20).plain) == 20


def test_spark_levels_padding_and_clamp() -> None:
    assert spark([0.0, 0.5, 1.0], width=5).plain == "  ▁▅█"
    assert spark([5.0], width=1).plain == "█"
    assert spark([-1.0], width=1).plain == "▁"
    assert spark([], width=4).plain == "    "
    assert spark([0.1] * 50, width=8).plain == "▂" * 8  # only last `width` render


def test_overall_cpu_active_averages_cores() -> None:
    assert overall_cpu_active(make_sample()) == pytest.approx(0.5)
    empty = PowerSample(0, [], 0.0, 0.0, None, None, None, None, None)
    assert overall_cpu_active(empty) == 0.0


def test_format_helpers() -> None:
    assert fmt_mhz(1234) == "1.23 GHz"
    assert fmt_mhz(800) == "800 MHz"
    assert fmt_mw(None) == "—"
    assert fmt_mw(1500) == "1.50 W"
    assert fmt_mw(250) == "250 mW"


class ProcHarness(App):
    def compose(self) -> ComposeResult:
        yield ProcPanel(id="proc")


async def test_proc_table_keyed_updates() -> None:
    app = ProcHarness()
    async with app.run_test() as pilot:
        panel = app.query_one("#proc", ProcPanel)
        table = panel.query_one("#proc-table", DataTable)

        panel.update_rows(
            [
                ProcInfo(pid=10, name="alpha", cpu_percent=50.0, mem_mb=100.0),
                ProcInfo(pid=20, name="beta", cpu_percent=30.0, mem_mb=50.0),
                ProcInfo(pid=30, name="gamma", cpu_percent=10.0, mem_mb=25.0),
            ]
        )
        await pilot.pause()
        assert table.row_count == 3
        assert [table.get_row_at(i)[0] for i in range(3)] == ["10", "20", "30"]

        table.move_cursor(row=1)  # cursor on pid 20

        # beta spikes to the top, gamma exits, delta appears
        panel.update_rows(
            [
                ProcInfo(pid=20, name="beta", cpu_percent=90.0, mem_mb=55.0),
                ProcInfo(pid=10, name="alpha", cpu_percent=40.0, mem_mb=100.0),
                ProcInfo(pid=40, name="delta", cpu_percent=5.0, mem_mb=10.0),
            ]
        )
        await pilot.pause()
        assert [table.get_row_at(i)[0] for i in range(3)] == ["20", "10", "40"]
        cursor = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        assert cursor.value == "20", "cursor should follow the same PID across a re-sort"

        # cursor target disappearing must not crash
        panel.update_rows([ProcInfo(pid=10, name="alpha", cpu_percent=1.0, mem_mb=1.0)])
        await pilot.pause()
        assert table.row_count == 1


async def test_proc_table_sort_by_mem() -> None:
    app = ProcHarness()
    async with app.run_test() as pilot:
        panel = app.query_one("#proc", ProcPanel)
        panel.sort_by = "mem"
        panel.update_rows(
            [
                ProcInfo(pid=1, name="cpu-hog", cpu_percent=99.0, mem_mb=10.0),
                ProcInfo(pid=2, name="mem-hog", cpu_percent=1.0, mem_mb=500.0),
            ]
        )
        await pilot.pause()
        table = panel.query_one("#proc-table", DataTable)
        assert [table.get_row_at(i)[0] for i in range(2)] == ["2", "1"]


class MemHarness(App):
    def compose(self) -> ComposeResult:
        yield MemPanel(History(), id="mem")


async def test_mem_panel_renders() -> None:
    app = MemHarness()
    async with app.run_test() as pilot:
        panel = app.query_one("#mem", MemPanel)
        gib = 1024**3
        panel.history.ram_used.append(0.5)
        panel.mem = (
            SimpleNamespace(total=32 * gib, available=16 * gib, percent=50.0),
            SimpleNamespace(total=2 * gib, used=1 * gib, percent=50.0),
        )
        await pilot.pause()
        console = Console(width=60)
        with console.capture() as cap:
            console.print(panel.render())
        out = cap.get()
        assert "Memory" in out
        assert "16.0 / 32.0 GB used" in out
        assert "swap" in out


@pytest.mark.skipif(shutil.which("powermetrics") is None, reason="needs macOS powermetrics")
async def test_fake_sample_flows_to_panels_and_history() -> None:
    """A sample in the reader queue must reach panels, history, and the ANE ceiling."""
    app = MonMonApp(interval_ms=1000)
    # keep the real stream out so live samples can't race the injected one
    app.reader.start = lambda: None  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        app.reader.samples.put_nowait(make_sample(ane_mw=12_000.0))
        await pilot.pause(1.3)  # one _poll_power tick
        assert app.query_one("#cpu", CpuPanel).sample is not None
        assert list(app.history.gpu_active) == [pytest.approx(0.4)]
        assert list(app.history.cpu_active) == [pytest.approx(0.5)]
        assert list(app.history.package_mw) == [2500.0]
        # a 12 W draw must raise the ceiling above the 8 W default
        assert app._ane_ceiling_mw == 12_000.0
        assert app.query_one("#ane", AnePanel).ceiling_mw == 12_000.0


def hermetic_app() -> MonMonApp:
    """MonMonApp with the real powermetrics stream kept out."""
    app = MonMonApp(interval_ms=1000)
    app.reader.start = lambda: None  # type: ignore[method-assign]
    return app


@pytest.mark.skipif(shutil.which("powermetrics") is None, reason="needs macOS powermetrics")
async def test_pause_freezes_updates() -> None:
    app = hermetic_app()
    async with app.run_test() as pilot:
        app.reader.samples.put_nowait(make_sample())
        await pilot.pause(1.3)
        assert len(app.history.cpu_active) == 1

        await pilot.press("p")
        assert app._paused
        assert app.query_one("#summary", SummaryBar).paused
        app.reader.samples.put_nowait(make_sample())
        await pilot.pause(1.3)
        assert len(app.history.cpu_active) == 1, "history must freeze while paused"

        await pilot.press("p")
        app.reader.samples.put_nowait(make_sample())
        await pilot.pause(1.3)
        assert len(app.history.cpu_active) == 2


@pytest.mark.skipif(shutil.which("powermetrics") is None, reason="needs macOS powermetrics")
async def test_filter_show_type_clear() -> None:
    app = hermetic_app()
    async with app.run_test() as pilot:
        panel = app.query_one("#proc", ProcPanel)
        inp = panel.query_one("#proc-filter", Input)
        assert not inp.display

        await pilot.press("slash")
        assert inp.display
        assert inp.has_focus
        await pilot.press("x", "y")
        assert panel.filter_text == "xy"

        await pilot.press("escape")
        assert not inp.display
        assert panel.filter_text == ""


@pytest.mark.skipif(shutil.which("powermetrics") is None, reason="needs macOS powermetrics")
async def test_help_screen_opens_and_closes() -> None:
    app = hermetic_app()
    async with app.run_test() as pilot:
        await pilot.press("question_mark")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)


@pytest.mark.skipif(shutil.which("powermetrics") is None, reason="needs macOS powermetrics")
async def test_kill_asks_and_handles_gone_pid() -> None:
    app = hermetic_app()
    async with app.run_test() as pilot:
        await pilot.press("p")  # freeze polling so the fake row stays put
        panel = app.query_one("#proc", ProcPanel)
        panel.update_rows([ProcInfo(pid=99_999_999, name="ghost", cpu_percent=1.0, mem_mb=1.0)])
        await pilot.pause()

        await pilot.press("k")
        assert isinstance(app.screen, ConfirmKillScreen)
        await pilot.press("n")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmKillScreen)

        await pilot.press("k")
        assert isinstance(app.screen, ConfirmKillScreen)
        await pilot.press("y")  # pid doesn't exist -> "already exited" path, no crash
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmKillScreen)


@pytest.mark.skipif(shutil.which("powermetrics") is None, reason="needs macOS powermetrics")
async def test_horizontal_breakpoints() -> None:
    app = hermetic_app()
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        assert app.screen.has_class("-narrow")
    app2 = hermetic_app()
    async with app2.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app2.screen.has_class("-wide")


@pytest.mark.skipif(not can_run_powermetrics(), reason="needs passwordless powermetrics")
async def test_live_powermetrics_end_to_end() -> None:
    """Drive the real app against a live powermetrics stream."""
    app = MonMonApp(interval_ms=500)
    async with app.run_test() as pilot:
        cpu = app.query_one("#cpu", CpuPanel)
        for _ in range(16):  # powermetrics cold start can take a few seconds
            await pilot.pause(0.5)
            if cpu.sample is not None:
                break
        assert app.reader.is_alive()
        assert not app._stream_dead
        assert cpu.sample is not None, "no live sample arrived within ~8s"
        assert app.history.cpu_active
        # virtualized runners (VirtualMac2,1 on GitHub Actions) stream samples
        # but expose no cluster telemetry — assert content on real hardware only
        if not (cpu.sample.hw_model or "").startswith("Virtual"):
            assert cpu.sample.clusters


@pytest.mark.skipif(shutil.which("powermetrics") is None, reason="needs macOS powermetrics")
async def test_stream_state_is_surfaced() -> None:
    """Without cached sudo the stream dies and must be flagged; with it, no false alarm."""
    app = MonMonApp(interval_ms=1000)
    async with app.run_test() as pilot:
        await pilot.pause(2.5)  # a couple of _poll_power ticks
        summary = app.query_one("#summary", SummaryBar)
        if app.reader.is_alive():
            assert not app._stream_dead
            assert summary.stream_alive
        else:
            assert app._stream_dead
            assert not summary.stream_alive
            assert "powermetrics ended" in str(summary.render())
