# monmon

A terminal UI for watching Apple Silicon work: efficiency cores, performance
cores, GPU, and the Neural Engine (NPU) — plus the top processes driving load.

<p align="center">
  <img src="docs/screenshot.png" alt="monmon TUI showing E-cores, P-cores, GPU, ANE, and a live process table" width="900">
</p>

> **One-shot build note**: this entire project was generated in a single pass
> by **Claude Opus 4.7 (1M context)** running in Claude Code, from the prompt
> shown at the bottom of this README. No manual edits were made before the
> first successful run.

## Requirements

- macOS on Apple Silicon (M1 or newer)
- `sudo` access — Apple's `powermetrics` requires root
- Homebrew (for the recommended install path) or Python 3.12+ and
  [uv](https://github.com/astral-sh/uv) (for the source path)

## Install

### Homebrew (recommended)

```sh
brew install gavi/monmon/monmon
```

That's a shorthand for `brew tap gavi/monmon && brew install monmon`. It
pulls the formula from [`gavi/homebrew-monmon`][tap], creates an isolated
Python 3.12 virtualenv in `$(brew --prefix)/opt/monmon/libexec`, installs
all dependencies pinned to exact versions, and puts `monmon` on your `$PATH`.

[tap]: https://github.com/gavi/homebrew-monmon

### From source (for development)

```sh
git clone https://github.com/gavi/monmon.git
cd monmon
uv sync
uv run monmon
```

Checks (all run headless, no sudo needed — CI runs the same three):

```sh
uv run pytest        # parser fixtures + Textual UI tests
uv run ruff check .  # lint
uv run mypy          # typecheck
```

Parser tests run against recorded `powermetrics` samples in
`tests/fixtures/` — contributions of captures from other chips and macOS
versions are welcome; see `tests/fixtures/README.md`.

## Run

```sh
monmon                 # 1 s sample interval
monmon --interval 500  # 500 ms samples — snappier, more CPU overhead
monmon --help
```

The first time you launch, macOS prompts for your password so `sudo` can
start `powermetrics`. The credential is cached for the session, so subsequent
runs in the same shell skip the prompt.

If the in-TUI password prompt fails (common on TouchID-only setups where
subprocess sudo can't trigger the biometric dialog), cache your credential
in the shell first:

```sh
sudo -v && monmon
```

Keys: `s` toggles the process table between CPU and memory sort; `q` or
`ctrl-c` quits.

### Optional: skip the password prompt

`powermetrics` is read-only telemetry, so it is reasonable to allow it — and
only it — through sudo without a password:

```sh
echo "$(whoami) ALL=(root) NOPASSWD: /usr/bin/powermetrics" | sudo tee /etc/sudoers.d/powermetrics
sudo chmod 440 /etc/sudoers.d/powermetrics
sudo visudo -c
```

monmon detects the rule and starts without prompting. Undo it any time with
`sudo rm /etc/sudoers.d/powermetrics`.

## Upgrade / uninstall

```sh
brew update && brew upgrade monmon    # pull in new releases
brew uninstall monmon                  # remove
brew untap gavi/monmon                 # drop the tap too
```

## What you see

- **CPU panel**: every E-core and P-core cluster with per-core active-residency
  bars and current frequency, plus a rolling overall-load sparkline.
  E-clusters render in cyan, P-clusters in magenta.
- **GPU panel**: active residency, frequency, power draw, and history.
- **NPU panel**: Apple Neural Engine power. `powermetrics` doesn't expose an
  "active" counter for ANE, so monmon treats any non-trivial power draw as
  "in use" and scales the bar against an 8 W ceiling that self-calibrates
  upward if your chip draws more.
- **Memory panel**: RAM used vs total, swap, and history.
- **Process table**: top processes by CPU or memory from `psutil` — press
  `s` to toggle the sort key. (powermetrics' task sampler is unreliable
  across macOS versions, so we use psutil for the "what's running?" view.)
- **Summary bar**: chip model, core counts, and package power with a
  sparkline scaled to the session's peak.

## Data source

Everything above comes from a single `powermetrics -f plist` stream with the
`cpu_power`, `gpu_power`, and `ane_power` samplers. Samples are NUL-delimited
XML plists; see `src/monmon/power.py` for the parser.

## Origin prompt

This is the exact prompt that produced the project, verbatim:

```
lets build a mac monitoring system that shows gpu
lets use
uv for package management and tui
we need to see e-cores and p-cores and also gpu and npu to see what is being run
go ahead a build it
```

Model: **Claude Opus 4.7 (1M context)** via Claude Code. One-shot — the only
follow-up was a cosmetic request to add this section and a `CLAUDE.md`.
