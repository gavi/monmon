# CLAUDE.md

Guidance for Claude Code when working on this repository.

## What monmon is

A macOS TUI system monitor for Apple Silicon. It reads Apple's
`powermetrics` via a `sudo` subprocess and renders E-cores, P-cores, GPU, and
the Neural Engine (ANE/NPU) in a Textual UI, alongside a psutil-backed process
table.

The full app was one-shotted by Claude Opus 4.7 (1M context). If you are
extending it, preserve that single-source-of-truth spirit: small, direct
modules, minimal indirection.

## Tooling

- Package manager: **uv** (do not switch to pip/poetry).
- Python: 3.12+.
- UI: **Textual** (`textual>=8.2`). Rich is pulled in transitively.
- System calls: **psutil** for per-process, **powermetrics** for hardware.

Build / run:

```sh
uv sync
uv run monmon              # launch TUI
uv run monmon -i 500       # 500 ms samples
```

There is **no build step for production** — this is a CLI installed via uv.
Do not run `npm run build` or any bundler here.

## Repo layout

```
src/monmon/
  __init__.py   # version
  __main__.py   # CLI entry (argparse, sudo bootstrap)
  app.py        # Textual App + widgets
  power.py      # powermetrics subprocess, NUL-delimited plist parser
  procs.py     # psutil process snapshot
```

Entry point wired via `[project.scripts] monmon = "monmon.__main__:main"` in
`pyproject.toml`. The package is laid out under `src/` and built with
`hatchling`.

## Design constraints worth preserving

- **One powermetrics stream**: the app spawns exactly one `powermetrics`
  subprocess on startup and holds it open for the life of the TUI. Do not
  create a new subprocess per sample — the cold-start latency is huge.
- **Plist format, not text**: `powermetrics -f plist` emits XML plists
  separated by NUL bytes. `_iter_plist_blocks` splits on NUL; `parse_sample`
  parses each block. If you add samplers, extend `parse_sample` rather than
  running a second instance.
- **sudo policy**: powermetrics requires root. `__main__.py` calls `sudo -v`
  interactively once, then `PowermetricsReader` spawns `sudo -n powermetrics`.
  Do not prompt for the password inside the TUI — it breaks the Textual
  render loop. If `sudo -v` fails (e.g. TouchID + subprocess quirks), instruct
  the user to run `sudo -v && uv run monmon`.
- **Reactive updates**: widget state is driven by Textual `reactive`
  attributes updated from a polling interval that drains the sample queue.
  Keep the powermetrics parse thread off the UI thread.
- **ANE has no residency counter**: we approximate ANE utilization from power
  draw with an 8 W ceiling. Don't pretend we have an active-ratio — document
  the approximation if you change the ceiling.
- **Process data comes from psutil, not powermetrics**: powermetrics' task
  sampler is inconsistent across macOS versions and its plist output for
  per-task data is fragile. Keep the two data sources separate.

## Safety rails

- Do not commit unless the user explicitly asks.
- Do not add Co-Authored-By lines to commits (per user global preferences).
- Keep the feature set tight: this is a monitor, not a profiler. Resist the
  urge to add thermal/battery/network sampling unless asked — the stack is
  meant to stay under ~500 lines.
- Do not introduce alternate permission schemes (helper tools, launchd jobs,
  setuid binaries) without explicit sign-off. The current sudo-in-shell model
  is intentional: simple, auditable, standard.

## Known rough edges

- The ANE power ceiling (8 W) is a guess based on M-series chip datasheets.
  Tune per-chip if you have ground truth.
- On some macOS versions, cluster names vary (`E-Cluster`, `E0-Cluster`,
  `ECPU`). `_cluster_kind` matches on the first letter; broaden if you see
  `?` kinds in the TUI.
- `sudo -v` from a subprocess occasionally fails on TouchID-only setups. The
  README documents the `sudo -v && uv run monmon` workaround.
