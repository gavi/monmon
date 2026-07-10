"""CLI entry point for `monmon`."""

from __future__ import annotations

import argparse
import subprocess
import sys

from . import __version__
from .app import run
from .power import can_run_powermetrics


def _prompt_sudo() -> bool:
    """Acquire a cached sudo credential interactively before starting the TUI."""
    print("monmon reads Apple's powermetrics, which requires root.")
    print("You'll be prompted for your password once; it will be cached for this session.\n")
    try:
        r = subprocess.run(["sudo", "-v"])
    except KeyboardInterrupt:
        return False
    return r.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="monmon",
        description="macOS silicon monitor — E/P cores, GPU, and NPU (Apple Neural Engine).",
    )
    parser.add_argument(
        "-i", "--interval",
        type=int, default=1000,
        help="powermetrics sample interval in ms (default: 1000)",
    )
    parser.add_argument("--version", action="version", version=f"monmon {__version__}")
    args = parser.parse_args(argv)

    if sys.platform != "darwin":
        print("monmon only runs on macOS (Apple Silicon recommended).", file=sys.stderr)
        return 2

    if not can_run_powermetrics():
        if not _prompt_sudo():
            print("Aborted: sudo credential not granted.", file=sys.stderr)
            return 1

    try:
        run(interval_ms=args.interval)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
