#!/usr/bin/env python3
"""CRUD API over the sticky-note journal's Non-WI ("Others") tasks.

A small command-line API for other processes to manage Non-WI tasks without
opening the widget UI:

    sticky_note_task.py put    '<json>'     add a task; prints its UUID
    sticky_note_task.py get    [<uuid>]     print one task as JSON, or (no uuid)
                                            a JSON array of every Non-WI task
    sticky_note_task.py update '<json>'     modify a task; prints it as JSON
    sticky_note_task.py delete <uuid>       remove a task; prints {"deleted":...}

Bodies are JSON strings:
    put:    {"title": "<required>", "comment": "<optional>"}
    update: {"id": "<required-uuid>", "title": "...", "comment": "...",
             "checked": true|false}   # omit any field to leave it unchanged
("comments" is accepted as an alias for "comment" on input.)

The GET / UPDATE task JSON is:
    {"uuid": "...", "title": "...", "comment": "...", "checked": true|false}
where `checked` reflects whether the task is completed.

Every mutation is written to today's journal (created/bootstrapped if absent)
under a cross-process file lock, mirroring the widget's own behavior — deletes
tombstone the task so carry-forward can't resurrect it. After a successful
mutation the running widget (if any) is signalled to refresh immediately.

Reuses the widget's own config + journal modules so the on-disk format always
matches what the widget reads and writes.

Exit codes:
    0  success
    2  usage / bad-JSON / validation error
    3  config error (e.g. unparseable config.yaml)
    4  task not found
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

# Make sibling widget modules importable whether run from the source dir or the
# install dir (~/.local/share/sticky-note/), regardless of the caller's cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sticky_note_config as cfgmod
from sticky_note_control import send_command
from sticky_note_journal import OthersTask, bootstrap_journal, load_journal, save_journal


class TaskError(Exception):
    """A user-facing error carrying the process exit code to return."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _journal_paths(cfg: "cfgmod.StickyNoteConfig") -> "tuple[str, str]":
    """Return (today_path, yesterday_path) for the configured journal dir."""
    jd = cfg.journal_dir()
    today = date.today()
    today_path = str(jd / f"{today.strftime('%Y-%m-%d')}-journal.md")
    yesterday_path = str(jd / f"{(today - timedelta(days=1)).strftime('%Y-%m-%d')}-journal.md")
    return today_path, yesterday_path


def _signal_refresh(cfg: "cfgmod.StickyNoteConfig") -> None:
    """Best-effort: tell a running widget to reload the journal immediately.

    Silent no-op if the widget isn't running or the signal fails — the change is
    already persisted and shows on the next auto-refresh regardless.
    """
    with contextlib.suppress(OSError):
        send_command(cfg.socket_port, {"cmd": "refresh", "scope": "journal"})


@contextlib.contextmanager
def _journal_txn(cfg: "cfgmod.StickyNoteConfig"):
    """Yield today's journal under an exclusive cross-process lock.

    Serialized against other external callers via an flock on a lock file next
    to the journal so concurrent mutations can't lose each other. On clean exit
    the journal is saved and a running widget is signalled to refresh.
    """
    today_path, yesterday_path = _journal_paths(cfg)
    lock_path = Path(today_path).with_suffix(".addtask.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        bootstrap_journal(today_path, yesterday_path)
        journal = load_journal(today_path)
        yield journal
        save_journal(today_path, journal)
    _signal_refresh(cfg)


def _find(journal, task_id: str) -> "OthersTask":
    """Return the task whose stable key matches task_id, or raise TaskError(4)."""
    for t in journal.others:
        if t.key == task_id:
            return t
    raise TaskError(f"task not found: {task_id}", 4)


def _task_json(task: "OthersTask") -> dict:
    return {
        "uuid": task.key,
        "title": task.text,
        "comment": task.comment,
        "checked": task.completed is not None,
    }


def _parse_body(raw: str) -> dict:
    """Parse a JSON object body, raising TaskError(2) on malformed input."""
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise TaskError(f"invalid JSON body: {exc}", 2)
    if not isinstance(obj, dict):
        raise TaskError("JSON body must be an object", 2)
    return obj


def _get_comment(body: dict) -> "str | None":
    """Read the comment from a body, accepting 'comment' or 'comments'.

    Returns None if neither key is present (caller decides whether that means
    'leave unchanged' or 'empty').
    """
    if "comment" in body:
        return body["comment"]
    if "comments" in body:
        return body["comments"]
    return None


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------

def cmd_put(cfg, body: dict) -> str:
    title = str(body.get("title") or "").strip()
    if not title:
        raise TaskError("'title' is required and must be non-empty", 2)
    comment = _get_comment(body)
    comment = "" if comment is None else str(comment)

    task_id = uuid.uuid4().hex
    with _journal_txn(cfg) as journal:
        journal.others.append(OthersTask(
            text=title, added=_now_iso(), id=task_id, comment=comment,
        ))
    return task_id


def cmd_get(cfg, task_id: "str | None") -> "dict | list":
    """Get one task by UUID, or every Non-WI task when task_id is omitted.

    With a UUID: returns that task's JSON (raises TaskError(4) if missing).
    Without one (None or empty): returns a JSON array of all Non-WI tasks in
    journal order.
    """
    task_id = (task_id or "").strip()
    # Read-only, but take the lock so we never observe a half-written journal.
    with _journal_txn(cfg) as journal:
        if not task_id:
            return [_task_json(t) for t in journal.others]
        return _task_json(_find(journal, task_id))


def cmd_update(cfg, body: dict) -> dict:
    task_id = str(body.get("id") or "").strip()
    if not task_id:
        raise TaskError("'id' is required", 2)

    # Validate before taking the lock so a bad request fails fast.
    new_title = None
    if "title" in body:
        new_title = str(body["title"] or "").strip()
        if not new_title:
            raise TaskError("'title', if given, must be non-empty", 2)
    new_comment = _get_comment(body)  # None => leave unchanged
    new_checked = None
    if "checked" in body:
        if not isinstance(body["checked"], bool):
            raise TaskError("'checked' must be a boolean", 2)
        new_checked = body["checked"]

    with _journal_txn(cfg) as journal:
        task = _find(journal, task_id)
        if new_title is not None:
            task.text = new_title
        if new_comment is not None:
            task.comment = str(new_comment)
        if new_checked is not None:
            if new_checked and task.completed is None:
                task.completed = _now_iso()
            elif not new_checked:
                task.completed = None
        return _task_json(task)


def cmd_delete(cfg, task_id: str) -> dict:
    task_id = task_id.strip()
    if not task_id:
        raise TaskError("a task UUID is required", 2)
    with _journal_txn(cfg) as journal:
        task = _find(journal, task_id)
        # Tombstone so carry-forward can't resurrect it (mirrors the widget UI).
        journal.removed_others.add(task.key)
        journal.others = [t for t in journal.others if t.key != task.key]
        return {"deleted": True, "id": task_id}


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sticky_note_task.py",
        description="CRUD API over the sticky-note journal's Non-WI tasks.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)
    p_put = sub.add_parser("put", help="add a task; prints its UUID")
    p_put.add_argument("body", help='JSON: {"title":"...","comment":"..."}')
    p_get = sub.add_parser(
        "get", help="print a task as JSON, or all tasks when no UUID is given"
    )
    p_get.add_argument("uuid", nargs="?", help="task UUID; omit to list all tasks")
    p_upd = sub.add_parser("update", help="modify a task; prints it as JSON")
    p_upd.add_argument("body", help='JSON: {"id":"...","title":"...",'
                                    '"comment":"...","checked":true|false}')
    p_del = sub.add_parser("delete", help="remove a task")
    p_del.add_argument("uuid", help="task UUID")
    args = parser.parse_args(argv)

    try:
        cfg = cfgmod.load_config()
    except cfgmod.ConfigError as exc:
        print(f"[sticky-note] config error: {exc}", file=sys.stderr)
        return 3

    try:
        if args.verb == "put":
            print(cmd_put(cfg, _parse_body(args.body)))
        elif args.verb == "get":
            print(json.dumps(cmd_get(cfg, args.uuid), ensure_ascii=False))
        elif args.verb == "update":
            print(json.dumps(cmd_update(cfg, _parse_body(args.body)), ensure_ascii=False))
        elif args.verb == "delete":
            print(json.dumps(cmd_delete(cfg, args.uuid), ensure_ascii=False))
    except TaskError as exc:
        print(f"[sticky-note] error: {exc}", file=sys.stderr)
        return exc.code
    return 0


if __name__ == "__main__":
    sys.exit(main())
