"""Background-agent launch + poll for the sticky-note widget.

Two responsibilities:

1. spawn_agent(name, args, wi=None) — launch `claude --agent <name>` as a
   detached background process using the settings resolved from config
   (agents.default merged with the named entry; unknown agent -> ConfigError
   with "agent <name> was not defined in config"). Returns immediately with a
   run id. Each run gets its own directory under RUNS_DIR holding meta.json
   (agent, args, pid, wi, state) plus out/err files; a small shell wrapper
   writes an atomic `status` file on completion so the poller can detect it
   without racing pid liveness.

2. poll_agents(journal_path) — scan the run registry for finished runs not yet
   consumed, and fold each finished agent's output into the journal:
   wi_state[<wi>].agent_state[<agent>] = {status, result, ...}. Called on a
   timer every `agent_poll_interval_minutes`. Takes the shared journal lock
   (passed in) so it can't clobber a concurrent GUS refresh or UI mutation.

The journal is the single source of truth for "activated agents": a run whose
meta records a `wi` writes its result there, so auto-refresh (which only
rewrites GUS-derived fields) preserves it.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import sticky_note_config as cfgmod
from sticky_note_journal import load_journal, save_journal

# Per-user run registry. Lives next to the journal-independent widget state so
# it survives widget restarts (crash-safe result recovery).
RUNS_DIR = Path(os.path.expanduser("~/.local/share/sticky-note/agent-runs"))

# Settings resolved by cfg.agent_config() that are NOT `claude` CLI flags and
# must be filtered out before building the argv.
#   timeout-minutes — enforced by the launcher wrapper, not passed to claude.
#   cwd             — the directory the agent runs in (see DEFAULT_AGENT_CWD).
_NON_CLI_KEYS = {"timeout-minutes", "cwd"}

# Where a widget-launched agent runs. Claude Code stores/looks up sessions PER
# project directory (keyed by a slug of the cwd), and the WI agents operate on
# the repo anyway (read local HTML sources, write files there). Running from the
# widget's install dir instead would (a) put files in the wrong place and
# (b) make `--resume <id>` fail with "No conversation found" because the session
# was created under the repo's project bucket, not the install dir's. Overridable
# per-agent via a `cwd:` setting in config.yaml.
DEFAULT_AGENT_CWD = os.path.expanduser("~/github/tcm-marketplace")

# Agent status vocabulary (written into journal wi_state[wi].agent_state[agent]):
#   running   — process alive, no result yet
#   done      — finished, exit 0
#   timed_out — killed by the timeout-minutes guard (exit 124)
#   failed    — finished with a non-zero, non-timeout exit code
#   lost      — process gone with no status file (killed -9 / crash)
# The panel maps these to icons; failed and timed_out share the failed icon.
STATUS_ICON = {
    "running": "resource/agent-running.png",
    "done": "resource/agent-done.png",
    "lost": "resource/agent-lost.png",
    "failed": "resource/agent-failed.png",
    "timed_out": "resource/agent-failed.png",
}
_FINISHED_STATUSES = ("done", "failed", "timed_out", "lost")


def icon_for_status(status: "str | None") -> "str | None":
    """Return the icon path (relative to the widget dir) for a status, or None."""
    return STATUS_ICON.get(status) if status else None


# Human-readable phrasing for the hover tooltip, one per status.
_STATUS_PHRASE = {
    "running": "is running",
    "done": "succeeded",
    "failed": "failed",
    "timed_out": "timed out",
    "lost": "lost",
}


def runs_of(entry) -> "list[dict]":
    """Normalize one agent_state value to a list of run dicts.

    A WI's agent may be invoked several times (e.g. a gated agent that stops for
    approval and is re-invoked to continue), so agent_state[agent] holds a LIST
    of run records, newest last. Legacy journals stored a single dict per agent;
    for back-compat that is treated as a one-element list."""
    if isinstance(entry, list):
        return [r for r in entry if isinstance(r, dict)]
    if isinstance(entry, dict):
        return [entry]
    return []


def _all_runs(agent_state: "dict | None") -> "list[tuple[str, dict]]":
    """Flatten agent_state into (agent_name, run_dict) pairs across every agent
    and every run, keeping only runs with a recognized status."""
    if not agent_state:
        return []
    out = []
    for name, entry in agent_state.items():
        for run in runs_of(entry):
            if run.get("status") in _STATUS_PHRASE:
                out.append((str(name), run))
    return out


def store_run(agent_state: dict, agent: str, entry: dict) -> bool:
    """Insert or update a run entry (matched by run_id) under agent_state[agent],
    migrating a legacy single-dict value to a list. Returns True if changed."""
    run_id = entry.get("run_id")
    runs = runs_of(agent_state.get(agent))
    for i, r in enumerate(runs):
        if r.get("run_id") == run_id:
            if r == entry:
                return False
            runs[i] = entry
            agent_state[agent] = runs
            return True
    runs.append(entry)
    agent_state[agent] = runs
    return True


def wi_agent_status_lines(agent_state: "dict | None") -> "list[str]":
    """Build the hover-tooltip lines for a WI's agent runs, one per run.

    Each line reads "<agent-name> <phrase>" (e.g. "tcm-wi-worker is running").
    Kept intentionally terse — the session id and full result live in the
    double-click details panel (wi_agent_details), not here.
    Ordering (per product spec): any still-running run first, then the rest by
    `started_at` descending (newest first). Runs with no recognized status are
    skipped. Returns [] when the WI has no agent runs.
    """
    items = _all_runs(agent_state)
    # running first; within each group, newest started_at on top.
    items.sort(key=lambda it: (
        0 if it[1].get("status") == "running" else 1,
        _neg_ts(it[1].get("started_at")),
    ))
    return [
        f"{name} {_STATUS_PHRASE[e['status']]}"
        for name, e in items
    ]


def _entry_result(entry: dict) -> str:
    """The agent's final text for a journal agent_state entry. `claude -p
    --output-format json` puts it in output['result']; when output is raw text
    (non-JSON / stderr tail) that text is the result. Empty string if absent."""
    output = entry.get("output")
    if isinstance(output, dict):
        return str(output.get("result") or "")
    if isinstance(output, str):
        return output
    return ""


def wi_agent_details(agent_state: "dict | None") -> "list[dict]":
    """Per-run detail records for the double-click read-only panel, one per run
    (an agent invoked multiple times contributes multiple records), same
    ordering as the hover tooltip (running first, then newest started_at first).
    Each record: {name, started_at, ended_at, status, session_id, result}.
    Returns [] when the WI has no agent runs."""
    items = _all_runs(agent_state)
    items.sort(key=lambda it: (
        0 if it[1].get("status") == "running" else 1,
        _neg_ts(it[1].get("started_at")),
    ))
    return [
        {
            "name": name,
            "started_at": e.get("started_at", ""),
            "ended_at": e.get("ended_at", ""),
            "status": e.get("status", ""),
            "session_id": _entry_session_id(e) or "",
            "result": _entry_result(e),
        }
        for name, e in items
    ]


def _entry_session_id(entry: dict) -> "str | None":
    """Session id for a journal agent_state entry. Prefers the top-level
    `session_id` (written by poll_agents), falling back to the id nested in the
    run's captured `output` dict so entries consumed before that field existed
    still surface it."""
    sid = entry.get("session_id")
    if not sid:
        output = entry.get("output")
        if isinstance(output, dict):
            sid = output.get("session_id")
    return str(sid) if sid else None


def _neg_ts(started_at: "str | None"):
    """Sort key that puts the most recent `started_at` first. Missing/unparseable
    timestamps sort last (treated as oldest)."""
    if not started_at:
        return float("inf")
    try:
        return -datetime.fromisoformat(started_at).timestamp()
    except ValueError:
        return float("inf")


def resolve_wi_agent_status(agent_state: "dict | None") -> "str | None":
    """Reduce a WI's per-agent state map to the single status the panel shows.

    Rule (per product spec): if ANY agent on the WI is still running, show
    `running` (blue). Otherwise show the status of the agent that ended last
    (by ended_at) — done / failed / timed_out / lost. Returns None when the WI
    has no agents.
    """
    entries = [run for _, run in _all_runs(agent_state)]
    if not entries:
        return None
    if any(e.get("status") == "running" for e in entries):
        return "running"
    finished = [e for e in entries if e.get("status") in _FINISHED_STATUSES]
    if not finished:
        return None
    latest = max(finished, key=lambda e: e.get("ended_at") or "")
    return latest.get("status")


def _pid_alive(pid: "int | None") -> bool:
    """True if the process is genuinely still running.

    A detached agent is a child of the widget, so when it exits it lingers as a
    zombie until reaped — and os.kill(pid, 0) SUCCEEDS on a zombie, which would
    make a dead agent look alive forever. So reap our children opportunistically
    first (WNOHANG, ignore ECHILD for runs orphaned across a widget restart),
    then probe: after reaping, a finished child's pid is gone and os.kill raises.
    """
    if not pid:
        return False
    pid = int(pid)
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False  # just reaped — it had exited
    except (ChildProcessError, OSError):
        pass  # not our child (orphaned/reparented) — fall through to the probe
    except (TypeError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _project_slug(cwd: str) -> str:
    """Claude Code's per-project storage slug for a working directory: the
    absolute path with every non-alphanumeric char replaced by '-'. Sessions
    launched from `cwd` live under ~/.claude/projects/<slug>/."""
    import re
    return re.sub(r"[^a-zA-Z0-9]", "-", os.path.abspath(os.path.expanduser(cwd)))


def _session_resolvable(session_id: str, cwd: str) -> bool:
    """True if `claude --resume <session_id>` run from `cwd` would find the
    session. Claude looks it up in the project bucket for that cwd, so a session
    created under a different directory is invisible and `--resume` fails fast
    with "No conversation found". Guards against blindly resuming a session that
    lives in another project (the cause of the widget's resume failures)."""
    if not session_id:
        return False
    proj = Path(os.path.expanduser("~/.claude/projects")) / _project_slug(cwd)
    return (proj / f"{session_id}.jsonl").exists()


def _run_dir(run_id: str) -> Path:
    return RUNS_DIR / run_id


def _build_claude_argv(name: str, settings: dict, prompt: str,
                       resume: "str | None" = None) -> list[str]:
    """Map resolved agent settings to a `claude -p` argv. Settings keys mirror
    CLI flags (model, effort, permission-mode, output-format, max-budget-usd);
    timeout-minutes is handled by the wrapper, not passed to claude. When
    `resume` is a session id, add `--resume <id>` so the run continues that
    session (e.g. a gated agent picking up after the user approved)."""
    argv = ["claude", "-p", prompt, "--agent", name]
    if resume:
        argv.extend(["--resume", str(resume)])
    for key, val in settings.items():
        if key in _NON_CLI_KEYS or val is None:
            continue
        argv.extend([f"--{key}", str(val)])
    return argv


def spawn_agent(name: str, args: "list[str] | None" = None, wi: "str | None" = None,
                resume: "str | None" = None) -> str:
    """Launch agent `name` in the background. Returns the run id immediately.

    args: extra string arguments appended to the prompt. The widget passes the
    prompt as the sole element for a single-argument agent; the list form is for
    agents that accept multiple positional arguments. Empty/None -> no args.

    resume: a prior run's claude session id. When set, the run continues that
    session (`claude --resume <id>`) instead of starting fresh.

    Raises cfgmod.ConfigError("agent <name> was not defined in config") when the
    agent is not listed under `agents` in config.yaml.
    """
    cfg = cfgmod.load_config()
    settings = cfg.agent_config(name)  # raises for unknown agent

    args = [str(a) for a in (args or [])]
    prompt = "\n".join(args)

    # Directory the agent runs in. Sessions are stored per project (keyed off
    # this cwd), so both a fresh run and any --resume must use the same dir.
    cwd = os.path.expanduser(str(settings.get("cwd") or DEFAULT_AGENT_CWD))
    if not os.path.isdir(cwd):
        print(f"[sticky-note] agent cwd {cwd} does not exist; "
              f"launching in the widget's directory instead", file=sys.stderr)
        cwd = None  # Popen inherits the widget's cwd

    # Only resume when the session actually lives in this cwd's project bucket;
    # otherwise `claude --resume` fails fast ("No conversation found"). Fall back
    # to a fresh run (drop --resume) rather than launching a doomed process.
    effective_cwd = cwd or os.getcwd()
    if resume and not _session_resolvable(str(resume), effective_cwd):
        print(f"[sticky-note] session {resume} not resolvable from {effective_cwd}; "
              f"starting {name} fresh instead of resuming", file=sys.stderr)
        resume = None

    try:
        timeout_min = int(settings.get("timeout-minutes", 0))
    except (TypeError, ValueError):
        timeout_min = 0

    run_id = f"{name}-{uuid.uuid4().hex[:12]}"
    rd = _run_dir(run_id)
    rd.mkdir(parents=True, exist_ok=True)

    argv = _build_claude_argv(name, settings, prompt, resume=resume)
    out_path = rd / "out.json"
    err_path = rd / "err.log"
    status_path = rd / "status"

    # Wrapper: run claude, capture its exit code, then atomically publish a
    # `status` file (write tmp + mv) so the poller never sees a half-written
    # status. `timeout` enforces the per-agent budget guard when configured.
    inner = " ".join(shlex.quote(a) for a in argv)
    if timeout_min > 0:
        # macOS ships neither `timeout` nor `gtimeout` by default (they come
        # from GNU coreutils). Use whichever exists; skip the guard otherwise
        # so a missing binary can't turn every run into an exit-127 failure.
        timeout_bin = shutil.which("timeout") or shutil.which("gtimeout")
        if timeout_bin:
            inner = f"{shlex.quote(timeout_bin)} {timeout_min * 60} {inner}"
        else:
            print("[sticky-note] no timeout/gtimeout on PATH; "
                  "agent timeout-minutes guard disabled", file=sys.stderr)
    wrapper = (
        f"{inner} > {shlex.quote(str(out_path))} 2> {shlex.quote(str(err_path))}; "
        f"code=$?; "
        f"printf '{{\"exit_code\": %d, \"ended_at\": \"%s\"}}' "
        f"\"$code\" \"$(date +%Y-%m-%dT%H:%M:%S)\" > {shlex.quote(str(status_path) + '.tmp')}; "
        f"mv {shlex.quote(str(status_path) + '.tmp')} {shlex.quote(str(status_path))}"
    )

    proc = subprocess.Popen(
        ["/bin/bash", "-c", wrapper],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=cwd,  # run in the repo so session storage + file writes line up
        start_new_session=True,  # detach: survives the widget, no zombie reaping
    )

    meta = {
        "run_id": run_id,
        "agent": name,
        "args": args,
        "wi": wi,
        "pid": proc.pid,
        "cwd": cwd,
        "state": "running",
        "started_at": _now_iso(),
    }
    (rd / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return run_id


def _read_json(path: Path) -> "dict | None":
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _collect_output(rd: Path) -> "str | dict":
    """Return the agent's output: parsed JSON if out.json is valid JSON,
    otherwise the raw text (or stderr tail if empty)."""
    out = (rd / "out.json")
    if out.exists():
        text = out.read_text(encoding="utf-8").strip()
        if text:
            try:
                return json.loads(text)
            except Exception:
                return text
    err = (rd / "err.log")
    if err.exists():
        return err.read_text(encoding="utf-8").strip()[-2000:]
    return ""


def _session_id(output: "str | dict | None") -> "str | None":
    """Extract claude's `session_id` from a finished run's output. `claude -p
    --output-format json` emits a top-level `session_id`; when the output is raw
    text (non-JSON / stderr tail) there's nothing to extract. Returns None if
    absent."""
    if isinstance(output, dict):
        sid = output.get("session_id")
        return str(sid) if sid else None
    return None


def poll_agents(journal_path: str) -> bool:
    """Fold every active/finished run into its WI's agent_state in the journal.

    For each un-consumed run:
      - no status file yet + pid alive  -> record `running` (kept for re-poll)
      - no status file yet + pid dead   -> record `lost`, consume the run
      - status file present, exit 0     -> `done`, consume
      - status file present, exit 124   -> `timed_out`, consume
      - status file present, other exit -> `failed`, consume

    Returns True if the journal changed (caller re-renders). A finished run is
    folded exactly once: meta.json is renamed meta.consumed.json. A running run
    keeps meta.json so its `running` marker is refreshed each poll and it can be
    finalized later.

    NOTE: the caller must hold the shared journal lock across this call.
    """
    if not RUNS_DIR.exists():
        return False

    changed = False
    journal = None

    def _journal():
        nonlocal journal
        if journal is None:
            journal = load_journal(journal_path)
        return journal

    for rd in sorted(RUNS_DIR.iterdir()):
        if not rd.is_dir():
            continue
        meta_path = rd / "meta.json"
        if not meta_path.exists():
            continue  # already consumed (meta.consumed.json) or malformed

        meta = _read_json(meta_path) or {}
        agent = meta.get("agent", "")
        wi = meta.get("wi")
        status = _read_json(rd / "status")

        if status is None:
            # No result yet. Either still running, or the process died without
            # writing a status file (kill -9 / crash) -> lost.
            if _pid_alive(meta.get("pid")):
                entry = {
                    "status": "running",
                    "run_id": meta.get("run_id", rd.name),
                    "started_at": meta.get("started_at", ""),
                }
                consume = False
            else:
                output = _collect_output(rd)
                entry = {
                    "status": "lost",
                    "run_id": meta.get("run_id", rd.name),
                    "started_at": meta.get("started_at", ""),
                    "ended_at": _now_iso(),
                    "output": output,
                    "session_id": _session_id(output),
                }
                consume = True
        else:
            exit_code = status.get("exit_code", -1)
            if exit_code == 0:
                st = "done"
            elif exit_code == 124:   # `timeout`/`gtimeout` SIGTERM exit code
                st = "timed_out"
            else:
                st = "failed"
            output = _collect_output(rd)
            entry = {
                "status": st,
                "exit_code": exit_code,
                "run_id": meta.get("run_id", rd.name),
                "started_at": meta.get("started_at", ""),
                "ended_at": status.get("ended_at", ""),
                "output": output,
                "session_id": _session_id(output),
            }
            consume = True

        if wi:
            state = _journal().wi_state.get(wi)
            if state is not None:
                # Match by run_id so each run keeps its own record (an agent may
                # be invoked multiple times on the same WI).
                if store_run(state.agent_state, agent, entry):
                    changed = True
            elif consume:
                # WI gone from the journal (purged/closed). Drop the result but
                # still consume so we don't reprocess it.
                print(f"[sticky-note] agent {agent} ({entry['status']}) for {wi} "
                      f"but WI is gone from journal; discarding result",
                      file=sys.stderr)

        if consume:
            meta["state"] = entry["status"]
            meta["consumed_at"] = _now_iso()
            (rd / "meta.consumed.json").write_text(json.dumps(meta), encoding="utf-8")
            meta_path.unlink(missing_ok=True)

    if changed and journal is not None:
        save_journal(journal_path, journal)
    return changed
