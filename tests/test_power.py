"""Parser tests: every fixture in tests/fixtures/ plus targeted edge cases."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from monmon.power import _cluster_kind, _iter_plist_blocks, parse_sample

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURES = sorted(FIXTURE_DIR.glob("*.plist"))


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_invariants(path: Path) -> None:
    """Any recorded sample must parse into sane, bounded values."""
    s = parse_sample(path.read_bytes())
    assert s.elapsed_ns > 0
    assert s.clusters
    for cluster in s.clusters:
        assert cluster.kind in {"E", "P"}, f"unrecognized cluster name {cluster.name!r}"
        assert 0.0 <= cluster.active <= 1.0
        assert cluster.freq_mhz >= 0.0
        assert cluster.cores
        for core in cluster.cores:
            assert core.cpu_id >= 0
            assert 0.0 <= core.active <= 1.0
    assert 0.0 <= s.gpu_active <= 1.0
    for power in (s.gpu_power_mw, s.ane_power_mw, s.cpu_power_mw, s.package_power_mw):
        assert power is None or power >= 0.0


def test_power_keys_pass_through_as_mw() -> None:
    s = parse_sample((FIXTURE_DIR / "m2pro-power-keys.plist").read_bytes())
    assert s.hw_model == "Mac14,10"
    assert [c.kind for c in s.clusters] == ["E", "P", "P"]
    assert len(s.clusters[0].cores) == 4
    assert s.cpu_power_mw == 1830.5
    assert s.gpu_power_mw == 412.0
    assert s.ane_power_mw == 0.0
    assert s.package_power_mw == 2242.5
    assert s.gpu_active == pytest.approx(0.09)
    assert s.gpu_freq_mhz == pytest.approx(888.0)


def test_energy_keys_convert_to_mw() -> None:
    """mJ over the sample window must divide by the window, not pass through."""
    s = parse_sample((FIXTURE_DIR / "energy-keys.plist").read_bytes())
    assert s.elapsed_ns == 500_000_000
    assert s.cpu_power_mw == pytest.approx(1200.0)
    assert s.gpu_power_mw == pytest.approx(200.0)
    assert s.ane_power_mw == pytest.approx(500.0)
    assert s.package_power_mw == pytest.approx(2000.0)


def test_macos27_real_capture_values() -> None:
    """Real Mac15,9 (M3 Max) capture on macOS 27 — pins that version's quirks."""
    s = parse_sample((FIXTURE_DIR / "mac15-9-m3max-macos27.plist").read_bytes())
    assert s.hw_model == "Mac15,9"
    assert [c.kind for c in s.clusters] == ["E", "P", "P"]
    assert [len(c.cores) for c in s.clusters] == [4, 6, 6]
    # macOS 27 reports GPU freq already in MHz despite the freq_hz key
    assert s.gpu_freq_mhz == pytest.approx(749.789)
    assert s.package_power_mw == pytest.approx(1726.53)
    assert s.cpu_power_mw == 0.0  # macOS 27 reports zero cpu_power; known quirk
    # cluster active comes from the core mean, not whole-cluster power-gating
    # (which reads 0 idle -> 100% active whenever anything runs)
    assert 0.0 < s.clusters[0].active < 1.0


def test_freq_unit_heuristic() -> None:
    doc: dict = {"elapsed_ns": 1, "processor": {"clusters": []}, "gpu": {"freq_hz": 749.789}}
    assert parse_sample(plistlib.dumps(doc)).gpu_freq_mhz == pytest.approx(749.789)
    doc["gpu"]["freq_hz"] = 888_000_000
    assert parse_sample(plistlib.dumps(doc)).gpu_freq_mhz == pytest.approx(888.0)


def test_down_ratio_counts_as_inactive() -> None:
    """A power-gated core reads idle=0/down=1 on macOS 27 — that is 0% active."""
    doc = {
        "elapsed_ns": 1,
        "processor": {
            "clusters": [
                {
                    "name": "P1-Cluster",
                    "freq_hz": 0,
                    "idle_ratio": 0.0,
                    "cpus": [
                        {"cpu": 10, "freq_hz": 0, "idle_ratio": 0.0, "down_ratio": 1.0},
                        {"cpu": 11, "freq_hz": 0, "idle_ratio": 0.2, "down_ratio": 0.7},
                    ],
                }
            ]
        },
        "gpu": {},
    }
    s = parse_sample(plistlib.dumps(doc))
    cores = s.clusters[0].cores
    assert cores[0].active == 0.0
    assert cores[1].active == pytest.approx(0.1)
    assert s.clusters[0].active == pytest.approx(0.05)


def test_cluster_active_is_core_mean() -> None:
    doc = {
        "elapsed_ns": 1,
        "processor": {
            "clusters": [
                {
                    "name": "E-Cluster",
                    "freq_hz": 1_000_000_000,
                    "idle_ratio": 0.0,
                    "cpus": [
                        {"cpu": 0, "freq_hz": 1_000_000_000, "idle_ratio": 0.5},
                        {"cpu": 1, "freq_hz": 1_000_000_000, "idle_ratio": 0.7},
                    ],
                }
            ]
        },
        "gpu": {},
    }
    s = parse_sample(plistlib.dumps(doc))
    assert s.clusters[0].active == pytest.approx(0.4)


def test_zero_window_energy_returns_none() -> None:
    doc = {"elapsed_ns": 0, "processor": {"clusters": [], "ane_energy": 100}, "gpu": {}}
    s = parse_sample(plistlib.dumps(doc))
    assert s.ane_power_mw is None


def test_missing_everything_parses() -> None:
    s = parse_sample(plistlib.dumps({}))
    assert s.clusters == []
    assert s.cpu_power_mw is None
    assert s.hw_model is None


def test_cluster_kind_name_variants() -> None:
    for name in ("E-Cluster", "E0-Cluster", "ECPU", "e-cluster"):
        assert _cluster_kind(name) == "E"
    for name in ("P-Cluster", "P1-Cluster", "PCPU"):
        assert _cluster_kind(name) == "P"
    assert _cluster_kind("CPU-Complex") == "?"


def test_iter_plist_blocks_reassembles_across_chunks() -> None:
    blocks = [b"<plist>one</plist>", b"<plist>two</plist>", b"<plist>three</plist>"]
    stream = b"\x00".join(blocks) + b"\x00"
    chunks = [stream[i : i + 7] for i in range(0, len(stream), 7)]
    assert list(_iter_plist_blocks(chunks)) == blocks


def test_iter_plist_blocks_yields_trailing_block_without_nul() -> None:
    assert list(_iter_plist_blocks([b"<plist>a</plist>\x00<plist>b"])) == [
        b"<plist>a</plist>",
        b"<plist>b",
    ]


def test_iter_plist_blocks_skips_blank_blocks() -> None:
    assert list(_iter_plist_blocks([b"\x00 \n\x00\x00<p/>\x00"])) == [b"<p/>"]
