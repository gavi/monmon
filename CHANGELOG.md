# Changelog

## 0.2.0 — 2026-07-10

### Added
- Rolling history sparklines for CPU load, GPU, ANE, package power, and RAM
- Memory panel: RAM used vs total, swap, history
- `s` key toggles the process table between CPU and memory sort
- `--version` flag
- Test suite (25 tests) with recorded powermetrics plist fixtures, including
  a real M3 Max / macOS 27 capture; ruff + mypy + GitHub Actions CI
- Passwordless operation via a powermetrics-scoped NOPASSWD sudoers rule
  (documented in the README); startup probes the actual capability instead
  of generic sudo

### Fixed
- Energy-key samples (`ane_energy` etc., mJ over the window) are now converted
  to mW using the sample window — previously read 2× low at 500 ms intervals
- GPU frequency on macOS 27, which reports MHz despite the `freq_hz` key
  (showed 0 MHz before)
- Cluster "avg" now averages per-core residency instead of whole-cluster
  power-gating, which reads 100% under any load on macOS 27
- A dead powermetrics stream is detected and surfaced in the UI instead of
  silently freezing on stale data (and the per-second `sudo -n true` probe
  on the render loop is gone)
- Process table updates rows in place with stable PID keys — the cursor
  follows the selected process across re-sorts
- ANE utilization ceiling self-calibrates to the session's peak draw instead
  of pegging at a fixed 8 W guess
- Quitting no longer prints sudo errors when only powermetrics is passwordless

## 0.1.0 — 2026-07-03

Initial release — E/P-core clusters, GPU, ANE, and process table in a
Textual TUI over a single `powermetrics` plist stream.
