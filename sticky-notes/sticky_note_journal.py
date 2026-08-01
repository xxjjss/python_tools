"""Journal read/write for the sticker widget.

Manages ~/Journal/YYYY-MM-DD-journal.md: YAML frontmatter (wi_state history)
+ Markdown body (Others task list). All writes are atomic via os.replace.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class WiHistory:
    category: str    # in_progress | done | todo
    entered_at: str  # ISO-8601 local time


@dataclass
class WiState:
    history: list[WiHistory] = field(default_factory=list)
    subject: str = ""
    # Per-agent state attached to this WI, keyed by agent name (e.g.
    # "tcm-wi-worker"). Each value is a free-form mapping written by a
    # background agent when it finishes (status/result/timestamps — shape TBD
    # by the presentation layer). Round-tripped verbatim so auto-refresh, which
    # only rewrites GUS-derived fields (history/subject), always preserves it.
    agent_state: dict = field(default_factory=dict)


@dataclass
class OthersTask:
    text: str
    added: str                  # ISO-8601
    completed: Optional[str] = None  # ISO-8601, or None if incomplete
    id: str = ""                # stable per-task id (legacy tasks have none)
    comment: str = ""           # optional free-form note (may be multi-line)

    @property
    def key(self) -> str:
        """Stable identity for carry-forward/dedup/tombstones.

        New tasks carry a real uuid; legacy tasks (written before ids existed)
        fall back to `added`, which is set once and carried forward verbatim,
        so it stays stable across days.
        """
        return self.id or self.added


@dataclass
class JournalData:
    wi_state: dict[str, WiState] = field(default_factory=dict)  # key = "W-XXXXXX"
    others: list[OthersTask] = field(default_factory=list)
    user_email: str = ""
    # Keys of Others tasks the user explicitly removed; suppresses carry-forward
    # resurrection. Pruned during bootstrap to only ids still in yesterday's file.
    removed_others: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_OTHERS_LINE_RE = re.compile(
    r"^- \[([ x])\] (.+?) <!-- added: ([^,>]+?)"
    r"(?:, id: ([^,>]+?))?"
    r"(?:, completed: ([^>]+?))? -->$"
)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (yaml_dict, body_after_frontmatter). Empty dict if no frontmatter."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    return yaml.safe_load(m.group(1)) or {}, text[m.end():]


def _parse_others(body: str) -> list[OthersTask]:
    tasks = []
    for line in body.splitlines():
        m = _OTHERS_LINE_RE.match(line.strip())
        if m:
            done, text, added, tid, completed = m.groups()
            tasks.append(OthersTask(
                text=text,
                added=added.strip(),
                completed=completed.strip() if completed else None,
                id=tid.strip() if tid else "",
            ))
    return tasks


def _wi_state_from_dict(raw: dict) -> dict[str, WiState]:
    result: dict[str, WiState] = {}
    for name, val in (raw or {}).items():
        if not isinstance(val, dict):
            val = {}
        history = [
            WiHistory(category=h["category"], entered_at=h.get("entered_at", ""))
            for h in val.get("history", [])
            if isinstance(h, dict) and "category" in h
        ]
        agent_state = val.get("agent_state")
        if not isinstance(agent_state, dict):
            agent_state = {}
        result[str(name)] = WiState(
            history=history,
            subject=val.get("subject", ""),
            agent_state=agent_state,
        )
    return result


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _wi_state_to_dict(wi_state: dict[str, WiState]) -> dict:
    result = {}
    for name, state in wi_state.items():
        entry: dict = {"history": [{"category": h.category, "entered_at": h.entered_at}
                                    for h in state.history]}
        if state.subject:
            entry["subject"] = state.subject
        if state.agent_state:
            entry["agent_state"] = state.agent_state
        result[name] = entry
    return result


def _others_to_lines(others: list[OthersTask]) -> list[str]:
    lines = []
    for task in others:
        check = "x" if task.completed else " "
        comment = f"<!-- added: {task.added}"
        if task.id:
            comment += f", id: {task.id}"
        if task.completed:
            comment += f", completed: {task.completed}"
        comment += " -->"
        lines.append(f"- [{check}] {task.text} {comment}")
    return lines


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_journal(path: str) -> JournalData:
    """Parse journal file. Raises on parse error so callers don't accidentally overwrite good data."""
    p = Path(os.path.expanduser(path))
    if not p.exists():
        return JournalData()
    text = p.read_text(encoding="utf-8")
    fm, body = _parse_frontmatter(text)
    wi_state = _wi_state_from_dict(fm.get("wi_state", {}))
    others = _parse_others(body)
    # Comments live in frontmatter (keyed by task key), not inline in the body:
    # they may be multi-line, which the single-line body format can't hold.
    raw_comments = fm.get("others_comments") or {}
    if isinstance(raw_comments, dict):
        for task in others:
            c = raw_comments.get(task.key)
            if isinstance(c, str) and c:
                task.comment = c
    raw_email = str(fm.get("user_email", "") or "")
    # Strip markdown link format if yaml stored it as [addr](mailto:addr)
    _email_m = re.match(r'^\[([^\]]+)\]\(mailto:[^\)]+\)$', raw_email)
    user_email = _email_m.group(1) if _email_m else raw_email
    removed_others = {str(k) for k in (fm.get("removed_others") or [])}
    return JournalData(
        wi_state=wi_state, others=others, user_email=user_email,
        removed_others=removed_others,
    )


def bootstrap_journal(today_path: str, yesterday_path: str) -> JournalData:
    """Create ~/Journal/ and today's file if absent. Carry forward incomplete Others."""
    today_p = Path(os.path.expanduser(today_path))
    today_p.parent.mkdir(parents=True, exist_ok=True)

    today_journal = load_journal(today_path)

    if not today_p.exists():
        save_journal(today_path, today_journal)

    # Carry forward from yesterday
    yesterday_p = Path(os.path.expanduser(yesterday_path))
    if yesterday_p.exists():
        yesterday_journal = load_journal(yesterday_path)
        # Carry forward user_email
        if not today_journal.user_email and yesterday_journal.user_email:
            today_journal.user_email = yesterday_journal.user_email
        # Carry forward wi_state so entered_at history is preserved across days
        # (avoids all WIs appearing blue/recently-entered on a new day)
        for name, state in yesterday_journal.wi_state.items():
            if name not in today_journal.wi_state:
                today_journal.wi_state[name] = state
        # Carry forward incomplete Others tasks by stable id (NOT text):
        # distinct same-text tasks are preserved, and a task the user explicitly
        # removed (tombstoned) is never resurrected. Tombstones are kept only as
        # long as yesterday still holds the matching task, then pruned.
        existing_keys = {t.key for t in today_journal.others}
        yesterday_keys = {t.key for t in yesterday_journal.others}
        for task in yesterday_journal.others:
            if (task.completed is None
                    and task.key not in existing_keys
                    and task.key not in today_journal.removed_others):
                today_journal.others.append(task)
                existing_keys.add(task.key)
        today_journal.removed_others &= yesterday_keys
        save_journal(today_path, today_journal)

    return today_journal


def diff_and_update(
    journal: JournalData,
    gus_items: list[dict],
    now: datetime,
) -> tuple[JournalData, bool]:
    """Compare current GUS categories against wi_state history.

    gus_items entries must have keys: name (str), category (str).
    category values: in_progress | done | todo | purge

    Returns (updated_journal, changed).
    """
    changed = False
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S")

    for item in gus_items:
        name: str = item["name"]
        category: str = item["category"]

        if category == "purge":
            if name in journal.wi_state:
                del journal.wi_state[name]
                changed = True
            continue

        subject: str = item.get("subject", "")
        state = journal.wi_state.get(name)

        if state is None:
            journal.wi_state[name] = WiState(
                history=[WiHistory(category=category, entered_at=now_iso)],
                subject=subject,
            )
            changed = True
        else:
            if not state.history or state.history[-1].category != category:
                state.history.append(WiHistory(category=category, entered_at=now_iso))
                changed = True
            if subject and state.subject != subject:
                state.subject = subject
                changed = True

    # Remove journal done entries absent from the (already-filtered) gus_items
    gus_names = {item["name"] for item in gus_items if item["category"] != "purge"}
    for name in [
        n for n, s in journal.wi_state.items()
        if s.history and s.history[-1].category == "done" and n not in gus_names
    ]:
        del journal.wi_state[name]
        changed = True

    return journal, changed


def save_journal(path: str, data: JournalData) -> None:
    """Atomically write journal to path (write .tmp then os.replace)."""
    p = Path(os.path.expanduser(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp path per write so concurrent writers never share/clobber it.
    tmp = p.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")

    fm: dict = {"wi_state": _wi_state_to_dict(data.wi_state)}
    if data.user_email:
        fm["user_email"] = data.user_email
    if data.removed_others:
        fm["removed_others"] = sorted(data.removed_others)
    others_comments = {t.key: t.comment for t in data.others if t.comment}
    if others_comments:
        fm["others_comments"] = others_comments
    frontmatter = yaml.dump(fm, default_flow_style=False, allow_unicode=True)

    date_str = p.stem.replace("-journal", "") if "-journal" in p.stem else p.stem
    others_lines = "\n".join(_others_to_lines(data.others))

    content = (
        f"---\n{frontmatter}---\n\n"
        f"# Journal — {date_str}\n\n"
        f"## Others Tasks\n\n"
        f"{others_lines}\n"
    )

    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, p)
