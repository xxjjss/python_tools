"""WI-agent invocation: launch background agents against a GUS work item.

Home for everything about running a `claude --agent <name>` job on one WI —
shared by the widget's auto-invoke (a WI entering current-sprint TODO) and the
manual double-click dialog. Kept separate from the AppKit window code so new
WI-related agents can be added here without touching sticky_note.py.

Adding a new WI agent:
  1. If it needs a special launch prompt (rather than the bare GUS URL), add its
     directive template to WI_AGENT_DIRECTIVES, keyed by agent name. The template
     is `.format(wi=<W-number>, url=<gus url>)`-ed at launch.
  2. List the agent under `agents:` in config.yaml so the widget may launch it.
  3. It then shows up automatically in the manual invoke dialog; auto-invoke
     policy (which agent fires unprompted on new TODOs) still lives in
     auto_invoke_wi_worker.
"""

from __future__ import annotations

import sys
from datetime import datetime

GUS_BASE_URL = "https://gus.lightning.force.com/lightning/r/ADM_Work__c/{id}/view"

# tcm-wi-worker gates every branch (Documentation / Action / Development) on an
# explicit human "confirm?" before it bumps GUS or dispatches. The widget
# launches it non-interactively (`claude -p`, permission-mode dontAsk) with no
# stdin, so a bare URL leaves the agent stalled at that gate forever — it
# classifies the WI and stops, never touching GUS. Pre-authorize the gate in the
# directive so a headless run can carry the WI through to the bump + dispatch.
_WI_WORKER_DIRECTIVE = (
    "wi={wi}\n"
    "url={url}\n"
    "This is a headless, non-interactive run launched by the sticky-note widget: "
    "there is no human to answer a confirmation prompt. Treat the Phase 4 "
    "confirmation gate as PRE-AUTHORIZED for this WI — proceed through classify -> "
    "bump to In Progress -> dispatch without asking. State the classification and "
    "the one-line evidence in your result. If the WI is genuinely unclassifiable "
    "(Unknown/uncertain), do NOT guess or start it: stop and report what is "
    "ambiguous instead."
)

# tcm-wi-researcher blocks at HUMAN GATE ① (Stage 1): it writes PLAN.md and waits
# for the user to reply "APPROVED" before drafting. A headless widget-launched run
# has nobody to approve, so it would stall at the plan gate. Auto-confirm ONLY that
# early gate so the run carries itself through Stages 0-2 (triage -> plan -> draft
# -> publish v1) unattended, then STOPS. Stage 3 (review/revision), Stage 4
# (presentation/handoff), and Stage 5 (closure) all require explicit human action
# and must NOT run in this headless invocation. It also stops early at any genuine
# blocker (missing identity, auth-blocked links, ambiguous scope) rather than
# guessing.
_WI_RESEARCHER_DIRECTIVE = (
    "wi={wi}\n"
    "url={url}\n"
    "This is a headless, non-interactive run launched by the sticky-note widget: "
    "there is no human to answer a confirmation prompt. Treat HUMAN GATE ① (the "
    "Stage 1 PLAN approval) as AUTO-CONFIRMED — write the plan, then proceed "
    "through research -> draft -> publish v1 (Stages 0-2) without waiting for an "
    "APPROVED reply. STOP after publishing v1: do NOT enter Stage 3 (review / "
    "revision loop), Stage 4 (presentation / handoff), or Stage 5 (closure) — "
    "those require explicit human action in a later interactive run. State the "
    "plan, the published v1 Doc URL, and what you produced in your result. Also "
    "STOP and report (do not guess) if you hit a genuine blocker: no resolvable "
    "identity, an auth-blocked linked doc, missing manifest keys, or a WI that is "
    "clearly not a Documentation-type deliverable."
)

# Per-agent launch directives. An agent with no entry here is launched with just
# the WI's GUS URL as its prompt. Register new WI agents that need a structured
# directive (like a pre-authorized gate) by adding a template keyed by name;
# it is formatted with `wi` (the W-number) and `url` (the WI's GUS URL).
WI_AGENT_DIRECTIVES: "dict[str, str]" = {
    "tcm-wi-worker": _WI_WORKER_DIRECTIVE,
    "tcm-wi-researcher": _WI_RESEARCHER_DIRECTIVE,
}


def _wi_agent_prompt(agent: str, wi_name: str, url: str) -> str:
    """Build the prompt/directive for a widget-launched agent run.

    Agents registered in WI_AGENT_DIRECTIVES get their structured directive
    (e.g. tcm-wi-worker's pre-authorized confirmation gate, since the run is
    headless and nobody can confirm). Any other agent just gets the WI's GUS URL.
    """
    directive = WI_AGENT_DIRECTIVES.get(agent)
    if directive is None:
        return url
    return directive.format(wi=wi_name, url=url)


def spawn_wi_agent(state, agent: str, wi_name: str, wi_id: str) -> bool:
    """Launch `agent` on a single WI, writing a running marker into its state.

    `state` is the WI's WiState (its agent_state map is mutated in place). The
    agent is spawned with the WI's GUS URL as its sole argument and `wi=wi_name`
    so poll_agents folds its result back here.

    Guard: only one run of the same agent per WI may be IN FLIGHT at a time —
    skip when a run is currently `running`. A finished run (done / failed /
    timed_out / lost) may be re-invoked: this is how a gated agent that stopped
    for the user's approval is continued. The re-invocation RESUMES the most
    recent finished run's claude session (`claude --resume <session_id>`) so the
    agent picks up where it left off rather than re-triaging from scratch; if no
    session id is recoverable it falls back to a fresh run.

    Each run is recorded separately (agent_state[agent] is a list of run records,
    newest last), so the details panel shows every invocation. On spawn a fresh
    `running` marker with a `started_at` timestamp is appended so the panel shows
    the blue icon immediately; poll_agents later stamps the terminal status onto
    that same run (matched by run_id).

    Returns True if a run was launched (state changed), False if skipped/failed.
    """
    import sticky_note_agents as agentsmod

    runs = agentsmod.runs_of(getattr(state, "agent_state", {}).get(agent))
    if any(r.get("status") == "running" for r in runs):
        return False  # a run is in flight — don't stack a duplicate

    # Resume the most recent finished run's session so a gated agent continues.
    resume_sid = None
    finished = [r for r in runs if r.get("session_id") or
                (isinstance(r.get("output"), dict) and r["output"].get("session_id"))]
    if finished:
        latest = max(finished, key=lambda r: r.get("ended_at") or r.get("started_at") or "")
        resume_sid = agentsmod._entry_session_id(latest)

    url = GUS_BASE_URL.format(id=wi_id)
    prompt = _wi_agent_prompt(agent, wi_name, url)
    try:
        run_id = agentsmod.spawn_agent(agent, args=[prompt], wi=wi_name, resume=resume_sid)
    except Exception as exc:
        print(f"[sticky-note] invoke {agent} for {wi_name} failed: {exc}",
              file=sys.stderr)
        return False
    # Append a new run record (keeps prior runs' history).
    agentsmod.store_run(state.agent_state, agent, {
        "status": "running",
        "run_id": run_id,
        "started_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "resumed_from": resume_sid or "",
    })
    print(f"[sticky-note] invoked {agent} for {wi_name} -> {run_id}"
          + (f" (resume {resume_sid})" if resume_sid else " (fresh)"), file=sys.stderr)
    return True


def auto_invoke_wi_worker(journal, filtered_gus, prev_categories, config) -> bool:
    """Spawn `tcm-wi-worker` for WIs newly entering the current-sprint TODO bucket.

    Called during a GUS refresh, AFTER diff_and_update has mutated `journal`.

    A WI qualifies when it is in `filtered_gus.todo_current` (TODO status AND its
    sprint contains today) and it was NOT already a TODO before this refresh
    (prev_categories[name] != "todo") — i.e. brand-new, or moved in from another
    bucket. The agent is launched with the WI's GUS URL as its sole argument.

    Guarded so at most one worker runs per WI AT A TIME, and a WI is processed
    to success at most once. The entry is skipped when its status is `running`
    (a run is already in flight) OR `done` (already succeeded — no point
    redoing it). A non-success terminal entry (failed / timed_out / lost) does
    NOT block a new run — a WI that re-enters current-sprint TODO after a
    previous worker failed, timed out, or was lost is retried. On spawn, a
    `running` marker (with a `started_at` timestamp) is written into agent_state
    so the panel shows the blue icon immediately and repeated refreshes before
    the next poll don't launch a duplicate; poll_agents later stamps `ended_at`
    and the terminal status onto the same entry.

    No-op (returns False) unless config.auto_invoke_wi_worker is true. Returns
    True if the journal changed (a marker was written) so the caller re-saves.
    """
    if not config.auto_invoke_wi_worker:
        return False

    changed = False
    for wi in filtered_gus.todo_current:
        if prev_categories.get(wi.name) == "todo":
            continue  # already a TODO before this refresh — not newly joined
        state = journal.wi_state.get(wi.name)
        if state is None:
            continue  # diff_and_update should have created it; be defensive
        if spawn_wi_agent(state, "tcm-wi-worker", wi.name, wi.id):
            changed = True
    return changed
