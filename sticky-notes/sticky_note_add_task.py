#!/usr/bin/env python3
"""Add a Non-WI task to the sticky-note journal from outside the widget.

Usage:
    sticky_note_add_task.py <title> [comment] [--id-only]
    sticky_note_add_task.py "Review PR #123" "detail: https://.../pr/123"

A thin convenience wrapper for the common "just add a task" case. The actual
work — writing the journal under a cross-process lock and signalling a running
widget to refresh — lives in sticky_note_task (the full put/get/update/delete
API); this simply builds a `put` body and calls it, so there is a single
implementation of the write path.

Prints the new task's UUID (the widget's stable key for the task). Pass
`--id-only` to print just the UUID on stdout, for scripting.

Exit codes:
    0  task added
    2  usage error (missing title)
    3  config error (e.g. unparseable config.yaml)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make sibling widget modules importable whether run from the source dir or the
# install dir (~/.local/share/sticky-note/), regardless of the caller's cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sticky_note_config as cfgmod
import sticky_note_task as taskmod


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sticky_note_add_task.py",
        description="Add a Non-WI task to the sticky-note journal.",
    )
    parser.add_argument("title", help="task title (the text shown in the list)")
    parser.add_argument(
        "comment", nargs="?", default="",
        help="optional comment (hover/double-click detail); may be multi-line",
    )
    parser.add_argument(
        "--id-only", action="store_true",
        help="print only the new task's UUID to stdout (for scripting)",
    )
    args = parser.parse_args(argv)

    title = args.title.strip()
    if not title:
        parser.error("title must not be empty")

    try:
        cfg = cfgmod.load_config()
    except cfgmod.ConfigError as exc:
        print(f"[sticky-note] config error: {exc}", file=sys.stderr)
        return 3

    # Delegate to the task API's `put` — it owns the locked write + refresh.
    try:
        task_id = taskmod.cmd_put(cfg, {"title": title, "comment": args.comment})
    except taskmod.TaskError as exc:
        print(f"[sticky-note] error: {exc}", file=sys.stderr)
        return exc.code

    if args.id_only:
        print(task_id)
    else:
        print(f"[sticky-note] added task: {title} (id: {task_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
