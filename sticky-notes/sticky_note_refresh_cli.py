#!/usr/bin/env python3
"""Signal the running sticky-note widget to refresh immediately.

Usage:
    sticky_note_refresh_cli.py [--scope journal|gus]

Sends a one-line JSON "refresh" command to the widget's loopback control socket
(127.0.0.1:<socket_port> from config.yaml). The widget re-renders at once
instead of waiting for its next auto-refresh tick.

    --scope journal  (default)  reload the local journal + re-render only (cheap;
                                use after an external journal edit like a task add)
    --scope gus                 force a full GUS re-query

Exit codes:
    0  refresh delivered
    1  widget not running (connection refused) or transport error
    3  config error
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sticky_note_config as cfgmod
from sticky_note_control import send_command


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sticky_note_refresh_cli.py",
        description="Signal the running sticky-note widget to refresh now.",
    )
    parser.add_argument(
        "--scope", choices=("journal", "gus"), default="journal",
        help="journal = reload local journal only (default); gus = full re-query",
    )
    args = parser.parse_args(argv)

    try:
        cfg = cfgmod.load_config()
    except cfgmod.ConfigError as exc:
        print(f"[sticky-note] config error: {exc}", file=sys.stderr)
        return 3

    try:
        resp = send_command(cfg.socket_port, {"cmd": "refresh", "scope": args.scope})
    except ConnectionRefusedError:
        print(
            f"[sticky-note] widget not running (nothing listening on "
            f"127.0.0.1:{cfg.socket_port})",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"[sticky-note] could not reach widget: {exc}", file=sys.stderr)
        return 1

    if not resp.get("ok"):
        print(f"[sticky-note] refresh rejected: {resp.get('error')}", file=sys.stderr)
        return 1
    print(f"[sticky-note] refresh delivered (scope={args.scope})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
