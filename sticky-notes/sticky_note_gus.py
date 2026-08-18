"""GUS data fetch and classification for the sticker widget.

Provides SOQL constants and classification logic mapping Status__c values
and sprint dates to the display buckets: in_progress, done, todo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class GusItem:
    id: str
    name: str            # W-XXXXXX
    subject: str
    status: str
    priority: str
    sprint_name: Optional[str]
    sprint_start: Optional[str]   # "YYYY-MM-DD" or None
    sprint_end: Optional[str]     # "YYYY-MM-DD" or None
    last_modified: str


@dataclass
class GusData:
    in_progress: list[GusItem] = field(default_factory=list)
    done: list[GusItem] = field(default_factory=list)
    todo_current: list[GusItem] = field(default_factory=list)
    todo_other: list[GusItem] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SOQL constants
# ---------------------------------------------------------------------------

QUERY_A = """SELECT Id, Name, Subject__c, Status__c, Priority__c,
       RecordType.DeveloperName, Sprint__r.Name, Sprint__r.Start_Date__c,
       Sprint__r.End_Date__c, Epic__r.Name, LastModifiedDate
FROM ADM_Work__c
WHERE Assignee__r.Email = 'CURRENT_USER_EMAIL'
  AND Status__c IN ('New', 'Triaged',
                    'In Progress', 'Ready for Review', 'Fixed', 'QA In Progress', 'Waiting')
ORDER BY Priority__c ASC, LastModifiedDate DESC
LIMIT 100"""

QUERY_B = """SELECT Id, Name, Subject__c, Status__c, Priority__c,
       RecordType.DeveloperName, LastModifiedDate
FROM ADM_Work__c
WHERE Assignee__r.Email = 'CURRENT_USER_EMAIL'
  AND Status__c IN ('Closed', 'Pending Release',
                    'Duplicate', 'Never', 'Not a Bug', 'Not Reproducible')
  AND LastModifiedDate >= LAST_N_HOURS:24
ORDER BY LastModifiedDate DESC
LIMIT 50"""

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Done = terminal resolutions. Includes the "won't-fix" family (Duplicated,
# Never, Not a Bug, Not Reproducible) alongside Closed/Pending Release.
_DONE_STATUSES = {
    "Closed", "Pending Release",
    "Duplicate", "Never", "Not a Bug", "Not Reproducible",
}
_TODO_STATUSES = {"New", "Triaged"}


def classify_wi(wi: GusItem, today: date) -> str:
    """Return category string: in_progress | done | todo.

    TODO and DONE are explicit allow-lists; everything else (In Progress,
    Ready for Review, Fixed, QA In Progress, Waiting, and any status not
    otherwise recognized) falls through to in_progress.
    """
    s = wi.status
    if s in _DONE_STATUSES:
        return "done"
    if s in _TODO_STATUSES:
        return "todo"
    return "in_progress"


def is_waiting(wi: GusItem) -> bool:
    return wi.status == "Waiting"


def is_current_sprint(wi: GusItem, today: date) -> bool:
    """True when the WI's sprint contains today."""
    if not wi.sprint_start or not wi.sprint_end:
        return False
    try:
        start = date.fromisoformat(wi.sprint_start)
        end = date.fromisoformat(wi.sprint_end)
        return start <= today <= end
    except ValueError:
        return False


def is_overdue(wi: GusItem, today: date) -> bool:
    """True when the WI has a sprint and that sprint ended before today."""
    if not wi.sprint_end:
        return False
    try:
        return date.fromisoformat(wi.sprint_end) < today
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _get(record: dict, *keys: str):
    """Safe nested dict access: _get(r, 'Sprint__r', 'Name') → value or None."""
    val = record
    for k in keys:
        if not isinstance(val, dict):
            return None
        val = val.get(k)
    return val


def parse_gus_response(records: list[dict]) -> list[GusItem]:
    """Map raw SOQL record dicts to GusItem list."""
    items = []
    for r in records:
        items.append(GusItem(
            id=r.get("Id", ""),
            name=r.get("Name", ""),
            subject=r.get("Subject__c", ""),
            status=r.get("Status__c", ""),
            priority=r.get("Priority__c") or "P4",
            sprint_name=_get(r, "Sprint__r", "Name"),
            sprint_start=_get(r, "Sprint__r", "Start_Date__c"),
            sprint_end=_get(r, "Sprint__r", "End_Date__c"),
            last_modified=r.get("LastModifiedDate", ""),
        ))
    return items


# ---------------------------------------------------------------------------
# Build GusData
# ---------------------------------------------------------------------------

def build_gus_data(
    query_a_records: list[dict],
    query_b_records: list[dict],
    today: date,
) -> GusData:
    """Classify all records and partition into GusData buckets."""
    gus = GusData()
    all_records = list(query_a_records) + list(query_b_records)
    items = parse_gus_response(all_records)

    for wi in items:
        cat = classify_wi(wi, today)
        if cat == "purge":
            continue
        if cat == "in_progress":
            gus.in_progress.append(wi)
        elif cat == "done":
            gus.done.append(wi)
        elif cat == "todo":
            if is_current_sprint(wi, today):
                gus.todo_current.append(wi)
            else:
                gus.todo_other.append(wi)

    return gus


def gus_items_for_diff(gus: GusData, purged_names: list[str]) -> list[dict]:
    """Flatten GusData + purge list into the format expected by diff_and_update."""
    items = []
    for wi in gus.in_progress:
        items.append({"name": wi.name, "subject": wi.subject, "category": "in_progress"})
    for wi in gus.done:
        items.append({"name": wi.name, "subject": wi.subject, "category": "done"})
    for wi in gus.todo_current + gus.todo_other:
        items.append({"name": wi.name, "subject": wi.subject, "category": "todo"})
    for name in purged_names:
        items.append({"name": name, "subject": "", "category": "purge"})
    return items
