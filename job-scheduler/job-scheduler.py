#!/usr/bin/env python3
"""job-scheduler — a tiny cron-style task runner.

Reads a YAML config that defines multiple jobs. Each job has one or more 5-field
cron expressions (`crons`) and a shell command. The scheduler ticks once per
minute and runs every job whose schedule matches the current local time. Jobs run
detached; a job that is still running when its next tick fires is skipped (no
overlap).

Usage:
    job-scheduler [run]            # start scheduler in background; print PID and exit
    job-scheduler run --foreground # run the scheduler loop in this process (for launchd)
    job-scheduler list             # print configured jobs and exit
    job-scheduler test <name>      # run one job by name immediately, then exit

Jobs file:
    job_scheduler.jobs_file in the shared ~/.fulcrum/fulcrum_config.yml
    ({logging.dir}/job_scheduler/jobs.yaml by default — see fulcrum_config_template.yml)

Config format — `crons` is always a LIST (one entry is fine); the job fires when
ANY entry matches:
    jobs:
      - name: example-poll
        crons:
          - "*/15 * * * *"
        command: /absolute/path/to/poll.sh
        enabled: true

`created_at` is stamped automatically at first registration (via `--put`) and is
never reset by later updates. `expire_at` is an optional duration ('30m', '2h',
'7d', '1w', or 'never' — default 'never') anchored at `created_at`; each time a
job's schedule matches (it "activates"), the scheduler checks expire_at first —
if past due, it logs and deletes the job instead of running it.

Several entries let one job run on several schedules — e.g. weekday daytime every
15 min, plus hourly on evenings and weekends:
    jobs:
      - name: multi
        crons:
          - "*/15 9-16 * * 1-5"
          - "0 17-21 * * *"
          - "0 * * * 0,6"
        on_overlap: densest   # error (default) | all | densest | sparsest
        command: /absolute/path/to/poll.sh

`on_overlap` decides what happens when two of a job's schedules have OVERLAPPING
TIME WINDOWS (same hour/day/month active — minute aside). Default is 'error':
    error     — reject the job; the user must choose a policy explicitly rather than
                silently pay for extra runs.
    all       — union: fire when ANY schedule matches (most runs).
    densest   — in an overlap window, only the densest schedule (most firings per
                active hour) decides which minutes fire; sparser overlapping
                schedules are muted.
    sparsest  — likewise, but only the sparsest schedule decides.
A job has one command, so this only picks WHICH minutes fire — never how many times
(one run per minute, always). A job with a single schedule never overlaps, so the
default never affects it.

Cron fields (5): minute hour day-of-month month day-of-week
Supports: *, */step, a-b ranges, a-b/step, and comma lists (e.g. "7,22,37,52").
day-of-week: 0-6 (Sun=0); 7 also accepted as Sunday.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import yaml  # type: ignore[import-not-found]
except ImportError:
    yaml = None

FULCRUM_CONFIG_PATH = Path.home() / ".fulcrum" / "fulcrum_config.yml"
DEFAULT_LOGGING_DIR = "~/.fulcrum/logs"
JOBS_FILE = "{logging.dir}/job_scheduler/jobs.yaml"
DEFAULT_LOG_FILE = "{logging.dir}/job_scheduler/job_scheduler.log"
LAUNCHD_LABEL = "com.fulcrum.job-scheduler"
RELOAD_REQUESTED = False


class ConfigError(Exception):
    """Recoverable config problem. Fatal for one-shot CLI commands, but the
    long-running loop catches it and keeps its last-good job set instead of dying
    (a crash under launchd KeepAlive would loop forever on the same bad config)."""


def load_fulcrum_config() -> dict:
    """Read the shared Fulcrum config, or {} if it's missing/unreadable. Every
    Fulcrum component reads its settings from this one installed file — see
    fulcrum_config_template.yml at the repo root for the full schema."""
    if yaml is None or not FULCRUM_CONFIG_PATH.is_file():
        return {}
    try:
        with FULCRUM_CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_template(value: str, fulcrum_cfg: dict) -> str:
    """Substitute the literal `{logging.dir}` placeholder with the shared config's
    logging.dir value."""
    if "{logging.dir}" not in value:
        return value
    logging_dir = (fulcrum_cfg.get("logging") or {}).get("dir") or DEFAULT_LOGGING_DIR
    return value.replace("{logging.dir}", logging_dir)


_LOGGER = None


def _get_logger():
    global _LOGGER
    if _LOGGER is not None:
        return _LOGGER
    import logging
    from logging.handlers import RotatingFileHandler

    fulcrum_cfg = load_fulcrum_config()
    js_cfg = fulcrum_cfg.get("job_scheduler") or {}
    log_file = Path(resolve_template(js_cfg.get("log_file", DEFAULT_LOG_FILE), fulcrum_cfg)).expanduser()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_cfg = fulcrum_cfg.get("logging") or {}
    handler = RotatingFileHandler(
        log_file,
        maxBytes=int(log_cfg.get("maxBytes", 1048576)),
        backupCount=int(log_cfg.get("backupCount", 10)),
        encoding=log_cfg.get("encoding", "utf-8"),
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger_cfg = (js_cfg.get("loggers") or {}).get("job_scheduler") or {}
    logger = logging.getLogger("job_scheduler")
    logger.addHandler(handler)
    logger.setLevel(logger_cfg.get("level", "INFO"))
    logger.propagate = bool(logger_cfg.get("propagate", False))
    _LOGGER = logger
    return logger


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)
    _get_logger().info(msg)


# ---- job age / expiry (see watch-pr's expire_at for the reference design) --
# job-scheduler runs entirely on naive local time (Cron matching uses
# datetime.now()), so timestamps here are local, not UTC — unlike watch-pr's
# "%Y-%m-%dT%H:%M:%SZ".
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"

_DURATION_RE = re.compile(r"(\d+)\s*([mhdw])", re.IGNORECASE)
_DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def _now_dt() -> datetime:
    return datetime.now()


def _now_str() -> str:
    return _now_dt().strftime(_TS_FORMAT)


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, _TS_FORMAT)
    except (ValueError, TypeError):
        return None


def parse_duration(spec: str):
    """Parse a duration like '7d', '2h', '1d12h30m', '1w', or 'never'.

    Units: m=minutes, h=hours, d=days, w=weeks. Returns total seconds (int),
    or None for 'never'. Raises ValueError on anything unparseable.
    """
    s = str(spec).strip().lower()
    if s == "never":
        return None
    # must be ONLY unit tokens, nothing left over
    if not s or _DURATION_RE.sub("", s).strip() != "":
        raise ValueError(
            f"bad duration {spec!r} (use e.g. 30m, 2h, 7d, 1w, 1d12h, or never)")
    total = 0
    for num, unit in _DURATION_RE.findall(s):
        total += int(num) * _DURATION_UNITS[unit.lower()]
    if total <= 0:
        raise ValueError(f"duration {spec!r} must be positive")
    return total


def _normalize_duration(spec: str) -> str:
    """Canonical spec string persisted to config: 'never' or the lowercased tokens."""
    s = str(spec).strip().lower()
    return "never" if s == "never" else s


def job_is_expired(job: dict, now: datetime) -> bool:
    """Check job['expire_at'] (a duration, anchored at job['created_at']) against
    `now`. Called at ACTIVATION (when a job's schedule matches and it's about to
    run) — see watch-pr's mode_watch self-expire check for the reference pattern.
    Unparseable/missing expire_at or missing created_at => never expires (fail
    open: a job's age must be well known before we auto-delete it)."""
    try:
        secs = parse_duration(job.get("expire_at", "never"))
    except ValueError:
        return False
    if secs is None:
        return False
    created = _parse_ts(job.get("created_at"))
    if created is None:
        return False
    return now >= created + timedelta(seconds=secs)


# ---- cron parsing ---------------------------------------------------------

def _parse_field(field: str, lo: int, hi: int) -> set:
    """Expand one cron field into the set of matching integers."""
    values: set = set()
    for part in field.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"step must be positive in {field!r}")
        else:
            base = part
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(base)
        if start < lo or end > hi or start > end:
            raise ValueError(f"field {field!r} out of range {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return values


class Cron:
    def __init__(self, expr: str):
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError(f"cron needs 5 fields, got {len(fields)}: {expr!r}")
        self.expr = expr
        self.minute = _parse_field(fields[0], 0, 59)
        self.hour = _parse_field(fields[1], 0, 23)
        self.dom = _parse_field(fields[2], 1, 31)
        self.month = _parse_field(fields[3], 1, 12)
        dow = _parse_field(fields[4], 0, 7)
        if 7 in dow:
            dow.discard(7)
            dow.add(0)  # both 0 and 7 mean Sunday
        self.dow = dow

    def day_ok(self, d) -> bool:
        # Standard cron semantics: if BOTH dom and dow are restricted (not the
        # full set), the job runs when EITHER matches. Otherwise both must match.
        dom_restricted = self.dom != set(range(1, 32))
        dow_restricted = self.dow != set(range(0, 7))
        py_dow = (d.weekday() + 1) % 7  # Mon=0..Sun=6  ->  Sun=0..Sat=6
        dom_ok = d.day in self.dom
        dow_ok = py_dow in self.dow
        if dom_restricted and dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def matches(self, dt: datetime) -> bool:
        return (
            dt.minute in self.minute
            and dt.hour in self.hour
            and dt.month in self.month
            and self.day_ok(dt)
        )

    def window_active(self, dt: datetime) -> bool:
        """True if this schedule's TIME WINDOW covers dt — hour/day/month match,
        ignoring the minute. This is the unit the on_overlap policy reasons over: two
        schedules 'overlap' when their windows are simultaneously active (e.g. an
        all-day */15 and a 5pm-9am schedule overlap during the evening), regardless of
        whether any single minute collides."""
        return (
            dt.hour in self.hour
            and dt.month in self.month
            and self.day_ok(dt)
        )


# A fixed leap year used as a representative calendar for overlap/density analysis
# (independent of "now"): a leap year covers all 12 months plus Feb 29, so every
# day-of-month, day-of-week, and month combination a cron can express is reachable.
_REF_YEAR = 2024


def window_density(cron: "Cron") -> int:
    """How densely this schedule fires WITHIN a single active hour: the number of
    minute-firings per hour, i.e. len(minute). This is the ranking key for
    densest/sparsest, and it is only ever compared between schedules whose window
    is active at the same minute (see job_should_fire) — so they share the current
    hour, and firings-in-this-hour is exactly len(minute).

    It is deliberately LOCAL to the overlap window, not an annual total: 'densest'
    must mean "fires most densely right here, right now", not "fires most over a
    year". A short, intense schedule like */5 10 * * 1-5 (12 firings in the 10am
    hour) is correctly denser at 10:xx than */15 9-16 * * 1-5 (4/hr), even though
    the latter racks up far more firings across the year."""
    return len(cron.minute)


def windows_overlap(a: "Cron", b: "Cron") -> bool:
    """True if two schedules' time windows can be active in the same minute — hour
    and month sets intersect, and some day of the reference year satisfies both day
    rules. Minute is deliberately ignored: this is a WINDOW test (see window_active),
    which is what on_overlap='error' rejects."""
    if not (a.hour & b.hour):
        return False
    if not (a.month & b.month):
        return False
    d = date(_REF_YEAR, 1, 1)
    end = date(_REF_YEAR + 1, 1, 1)
    while d < end:
        if (
            d.month in a.month and d.month in b.month
            and a.day_ok(d) and b.day_ok(d)
        ):
            return True
        d += timedelta(days=1)
    return False


# ---- config ---------------------------------------------------------------

def resolve_config() -> Path:
    """Resolve the jobs YAML path from JOBS_FILE (via shared fulcrum config)."""
    fulcrum_cfg = load_fulcrum_config()
    js_cfg = fulcrum_cfg.get("job_scheduler") or {}
    jobs_file = resolve_template(js_cfg.get("jobs_file", JOBS_FILE), fulcrum_cfg)
    return Path(jobs_file).expanduser()


def _config_format(cfg_path: Path) -> str:
    suffix = cfg_path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return "yaml"
    raise ConfigError(
        f"unsupported config file extension: {cfg_path.suffix or '(none)'} "
        "(supported: .yaml, .yml)"
    )


def load_config_data(cfg_path: Path) -> dict:
    if not cfg_path.exists():
        _config_format(cfg_path)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("jobs:\n", encoding="utf-8")
        log(f"job file created: {cfg_path}")
        return {"jobs": []}
    raw = cfg_path.read_text()
    _config_format(cfg_path)
    if yaml is None:
        raise ConfigError("PyYAML is required but not available in current Python env")
    try:
        data = yaml.safe_load(raw)
    except Exception as e:
        raise ConfigError(f"config is not valid YAML: {e}")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("config root must be an object")
    jobs = data.get("jobs")
    if jobs is None:
        data["jobs"] = []
    elif not isinstance(jobs, list):
        raise ConfigError("config field 'jobs' must be a list")
    return data


def _normalize_crons_field(raw) -> list[str]:
    """A job's 'crons' is a LIST of expression strings, so one job can run on one or
    several schedules (e.g. weekday-daytime AND weekend-hourly). Returns the list of
    raw expression strings, or raises ValueError on a bad shape."""
    if not isinstance(raw, list):
        raise ValueError(f"'crons' must be a list of strings, got {type(raw).__name__}")
    if not raw:
        raise ValueError("'crons' list is empty")
    for e in raw:
        if not isinstance(e, str):
            raise ValueError(f"each 'crons' entry must be a string, got {type(e).__name__}")
    return raw


OVERLAP_POLICIES = ("all", "error", "densest", "sparsest")
# Fail closed: a multi-schedule job whose windows overlap is REJECTED unless the
# user explicitly picks a policy. This forces a deliberate choice rather than
# silently running the command extra times (and paying for it). A job with a
# single schedule never overlaps, so this default is invisible to it.
_DEFAULT_OVERLAP = "error"


def _find_window_overlaps(crons: list["Cron"]) -> list[tuple[int, int]]:
    """Index pairs (i, j) whose time windows can be simultaneously active."""
    pairs = []
    for i in range(len(crons)):
        for j in range(i + 1, len(crons)):
            if windows_overlap(crons[i], crons[j]):
                pairs.append((i, j))
    return pairs


def check_overlap_policy(name: str, crons: list["Cron"], policy: str) -> None:
    """Config-time validation. Only 'error' rejects here: if two schedules' time
    windows can be active at once, the job is refused. 'all'/'densest'/'sparsest'
    are enforced at RUNTIME by job_should_fire — nothing to validate up front."""
    if policy != "error" or len(crons) < 2:
        return
    pairs = _find_window_overlaps(crons)
    if not pairs:
        return
    involved = sorted({i for pair in pairs for i in pair})
    exprs = ", ".join(f"{crons[i].expr!r}" for i in involved)
    raise ConfigError(
        f"job {name!r}: on_overlap='error' but schedule windows overlap: {exprs}"
    )


def job_should_fire(job: dict, now: datetime) -> bool:
    """Runtime decision: should this job's command run at `now`, under its on_overlap
    policy? A job has ONE command, so the policy only decides WHICH schedules get a
    say in a given minute — never how many times it runs (the per-tick call + the
    no-overlap guard already cap that at one).

      all       — union: fire if ANY schedule matches this minute.
      densest   — in each minute, only the densest schedule whose WINDOW is active
                  gets to trigger; sparser overlapping schedules are suppressed.
      sparsest  — likewise, but only the sparsest active-window schedule triggers.
      error     — behaves like 'all' at runtime (overlaps were rejected at load).
    """
    crons = job["crons"]
    policy = job.get("on_overlap", _DEFAULT_OVERLAP)
    if policy in ("all", "error") or len(crons) < 2:
        return any(c.matches(now) for c in crons)
    # densest / sparsest: among schedules whose WINDOW is active right now, keep only
    # the extreme by density; the job fires iff that one schedule matches this minute.
    active = [c for c in crons if c.window_active(now)]
    if not active:
        return False
    pick_max = policy == "densest"
    chosen = active[0]
    chosen_density = window_density(chosen)
    for c in active[1:]:
        d = window_density(c)
        # Ties resolve to the earlier schedule (already held in `chosen`).
        if (pick_max and d > chosen_density) or (not pick_max and d < chosen_density):
            chosen, chosen_density = c, d
    return chosen.matches(now)


def load_jobs(cfg_path: Path) -> list[dict]:
    data = load_config_data(cfg_path)
    jobs = data.get("jobs", [])
    parsed = []
    for i, job in enumerate(jobs):
        if not isinstance(job, dict):
            log(f"job entry #{i} is not a mapping ({type(job).__name__}) — skipping")
            continue
        name = job.get("name") or f"job-{i}"
        if not job.get("command"):
            log(f"job {name!r}: missing 'command' — skipping")
            continue
        if not job.get("crons"):
            log(f"job {name!r}: missing 'crons' — skipping")
            continue
        try:
            crons = [Cron(expr) for expr in _normalize_crons_field(job["crons"])]
        except ValueError as e:
            log(f"job {name!r}: bad crons {job['crons']!r}: {e} — skipping")
            continue
        policy = job.get("on_overlap", _DEFAULT_OVERLAP)
        if policy not in OVERLAP_POLICIES:
            log(f"job {name!r}: 'on_overlap' must be one of {OVERLAP_POLICIES}, got {policy!r} — skipping")
            continue
        try:
            check_overlap_policy(name, crons, policy)
        except ConfigError as e:
            # One offending job is skipped, not fatal — a bad reload must never take
            # the whole scheduler down (KeepAlive crash-loop). --put fails hard instead.
            log(f"{e} — skipping")
            continue
        raw_enabled = job.get("enabled", True)
        if isinstance(raw_enabled, bool):
            enabled = raw_enabled
        else:
            # Fail closed: a non-bool 'enabled' (e.g. the string "false", which is
            # truthy in Python) must NOT silently run the job. Treat as disabled.
            log(f"job {name!r}: 'enabled' must be true/false, got {raw_enabled!r} — treating as disabled")
            enabled = False
        expire_at = job.get("expire_at", "never")
        try:
            parse_duration(expire_at)
        except ValueError as e:
            log(f"job {name!r}: bad expire_at {expire_at!r}: {e} — skipping")
            continue
        parsed.append(
            {
                "name": name,
                "crons": crons,  # one or more schedules; job_should_fire applies on_overlap
                "on_overlap": policy,
                "command": job["command"],
                "enabled": enabled,
                "created_at": job.get("created_at"),  # set once at registration; see cmd_put
                "expire_at": _normalize_duration(expire_at),  # duration anchored at created_at; "never" default
                "proc": None,  # live subprocess.Popen while running
            }
        )
    return parsed


def save_config_data(cfg_path: Path, data: dict) -> None:
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    _config_format(cfg_path)
    if yaml is None:
        raise ConfigError("PyYAML is required but not available in current Python env")
    content = yaml.safe_dump(data, sort_keys=False, allow_unicode=False)
    # Atomic write: dump to a temp file in the same dir, fsync, then os.replace.
    # A SIGKILL mid-write leaves the temp file, never a truncated config — which,
    # combined with the SIGHUP the caller sends next, would crash-loop the scheduler.
    tmp = cfg_path.with_name(f".{cfg_path.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, cfg_path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


# Shell syntax that means the command isn't a bare "exe arg arg" — jobs run via
# shell=True, so in these cases the first shlex token is not the real executable
# (e.g. "cd /tmp && run.sh" -> "cd", a builtin `which` can't resolve).
_SHELL_METACHARS = set("&|;<>()$`\n")


def validate_command(command: str) -> tuple[bool, str]:
    command = command.strip()
    if not command:
        return False, "command is empty"
    try:
        parts = shlex.split(command)
    except ValueError as e:
        return False, f"command parse error: {e}"
    if not parts:
        return False, "command is empty after parsing"
    # If the command uses shell syntax, it's run by the shell — the resolvability
    # check below would false-reject builtins/pipelines that work fine in config.
    if any(c in _SHELL_METACHARS for c in command):
        return True, ""
    exe = parts[0]
    if "/" in exe:
        p = Path(exe).expanduser()
        if not p.exists():
            return False, f"executable not found: {p}"
        if not os.access(p, os.X_OK):
            return False, f"file is not executable: {p}"
        return True, ""
    if shutil.which(exe) is None:
        return False, f"executable not found in PATH: {exe}"
    return True, ""


def _request_reload(signum, frame) -> None:  # noqa: ARG001
    global RELOAD_REQUESTED
    RELOAD_REQUESTED = True


def _handle_termination(signum, frame) -> None:  # noqa: ARG001
    """Raise into the main thread so run_loop unwinds and the finally-block
    pidfile cleanup runs. A default SIGTERM (launchctl bootout, plain kill,
    systemctl stop) otherwise terminates at the C level — finally never runs,
    leaving a stale pidfile for a future recycled PID to collide with."""
    log(f"received signal {signum} — shutting down.")
    raise SystemExit(0)


def _proc_start_time(pid: int) -> str | None:
    """Best-effort, cross-platform process start time as an opaque token, used
    to detect PID reuse. Returns None if it can't be determined."""
    # Linux: field 22 of /proc/<pid>/stat is starttime (clock ticks since boot).
    # `comm` (field 2) may contain spaces/parens, so split after the final ')'.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        after = stat.rsplit(")", 1)[1].split()
        return after[19]  # field 22 overall == index 19 after the first two fields
    except (OSError, IndexError):
        pass
    # macOS/BSD: `ps -o lstart` is a stable per-process start timestamp.
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _pidfile_path(cfg_path: Path) -> Path:
    """A pidfile keyed to the config path. The running loop that owns this config
    records its pid here; --put/--delete read it to signal exactly that scheduler
    (not, as `pgrep -f` did, every same-user process whose args contain the script
    path — including a `test` run or the caller's own shell)."""
    import hashlib

    digest = hashlib.sha1(str(cfg_path.resolve()).encode()).hexdigest()[:12]
    base = Path(
        os.environ.get("XDG_RUNTIME_DIR")
        or (Path.home() / ".local" / "share" / "job-scheduler")
    )
    return base / f"scheduler-{digest}.pid"


def _write_pidfile(cfg_path: Path) -> Path | None:
    pf = _pidfile_path(cfg_path)
    pid = os.getpid()
    # Line 1: pid. Line 2: an opaque start-time token. The token lets a later
    # reader detect PID reuse — a stale pidfile whose PID the OS recycled for an
    # unrelated process won't match, so we never SIGHUP the wrong process.
    token = _proc_start_time(pid) or ""
    try:
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(f"{pid}\n{token}\n")
        return pf
    except OSError as e:
        log(f"could not write pidfile {pf}: {e} — --put/--delete won't hot-reload this scheduler")
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned differently — still a real process


def _launchd_target() -> str | None:
    """gui/<uid>/com.fulcrum.job-scheduler, or None when not on macOS."""
    if sys.platform != "darwin":
        return None
    return f"gui/{os.getuid()}/{LAUNCHD_LABEL}"


def _launchd_agent_loaded() -> bool:
    """True when the fulcrum job-scheduler LaunchAgent is currently loaded."""
    target = _launchd_target()
    if target is None:
        return False
    # `launchctl print` succeeds iff the service is present in the domain.
    return subprocess.run(
        ["launchctl", "print", target],
        capture_output=True,
    ).returncode == 0


def _restart_via_launchd(cfg_path: Path, old_pid: int) -> int | None:
    """Restart the KeepAlive agent with kickstart -k; return the new PID."""
    target = _launchd_target()
    if target is None:
        return None
    log(f"launchd agent {LAUNCHD_LABEL} is loaded — restarting via kickstart -k (not kill).")
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", target],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        log(f"launchctl kickstart failed: {err or f'exit {result.returncode}'}")
        return None
    # Wait until the old PID is gone and a (possibly new) live pidfile appears.
    for _ in range(40):
        time.sleep(0.25)
        if _pid_alive(old_pid):
            continue
        new_pid = _read_pidfile(cfg_path)
        if new_pid is not None and new_pid != old_pid:
            return new_pid
        # Same PID recycled is vanishingly rare within this window; accept any live owner.
        if new_pid is not None:
            return new_pid
    log(f"launchd restarted {LAUNCHD_LABEL}, but no live pidfile appeared for {cfg_path}")
    return None


def _stop_manual_scheduler(existing_pid: int) -> None:
    """SIGTERM a scheduler that is NOT under launchd KeepAlive."""
    log(f"stopping pid {existing_pid}...")
    try:
        os.kill(existing_pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        log(f"cannot stop pid {existing_pid}: permission denied.")
        sys.exit(1)
    for _ in range(10):
        if not _pid_alive(existing_pid):
            return
        time.sleep(0.5)
    log(f"pid {existing_pid} did not exit in time -- refusing to start a second scheduler.")
    sys.exit(1)


def _read_pidfile(cfg_path: Path) -> int | None:
    pf = _pidfile_path(cfg_path)
    try:
        lines = pf.read_text().splitlines()
        pid = int(lines[0].strip())
    except (OSError, ValueError, IndexError):
        return None
    recorded_token = lines[1].strip() if len(lines) > 1 else ""
    # Confirm the pid is actually alive before we trust a stale file.
    if not _pid_alive(pid):
        return None
    # Identity guard: if we recorded a start-time token, the live PID's current
    # start time must match. A mismatch means the PID was recycled — treat the
    # pidfile as stale rather than signaling an unrelated process.
    if recorded_token:
        current_token = _proc_start_time(pid)
        if current_token is not None and current_token != recorded_token:
            return None
    return pid


def notify_scheduler_reload(cfg_path: Path) -> None:
    pid = _read_pidfile(cfg_path)
    if pid is None:
        log("no running scheduler found for this config to reload")
        return
    try:
        os.kill(pid, signal.SIGHUP)
        log(f"requested config reload on scheduler pid {pid}")
    except ProcessLookupError:
        log("no running scheduler found for this config to reload")
    except PermissionError:
        log(f"cannot signal pid {pid}: permission denied")


def parse_put_job(raw: str) -> dict:
    try:
        job = json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"--put expects JSON object, parse failed: {e}")
        sys.exit(1)
    if not isinstance(job, dict):
        log("--put expects a JSON object, e.g. '{\"name\":\"x\",\"crons\":[\"*/5 * * * *\"],\"command\":\"echo hi\",\"enabled\":true}'")
        sys.exit(1)
    name = job.get("name")
    command = job.get("command")
    enabled = job.get("enabled", True)
    policy = job.get("on_overlap", _DEFAULT_OVERLAP)
    expire_at = job.get("expire_at", "never")

    if not isinstance(name, str) or not name.strip():
        log("job validation failed: 'name' must be a non-empty string")
        sys.exit(1)
    # 'crons' is a list of one or more expressions (multiple schedules).
    try:
        exprs = _normalize_crons_field(job.get("crons"))
    except ValueError as e:
        log(f"job validation failed: {e}")
        sys.exit(1)
    stripped = [e.strip() for e in exprs]
    crons = []
    for e in stripped:
        try:
            crons.append(Cron(e))
        except ValueError as err:
            log(f"job validation failed: bad cron {e!r}: {err}")
            sys.exit(1)
    if policy not in OVERLAP_POLICIES:
        log(f"job validation failed: 'on_overlap' must be one of {OVERLAP_POLICIES}, got {policy!r}")
        sys.exit(1)
    # Unlike a reload (which skips one bad job to stay alive), --put fails hard so
    # the user sees the overlap rejection immediately and nothing is written.
    try:
        check_overlap_policy(name.strip(), crons, policy)
    except ConfigError as e:
        log(f"job validation failed: {e}")
        sys.exit(1)
    if not isinstance(command, str):
        log("job validation failed: 'command' must be a string")
        sys.exit(1)
    ok, why = validate_command(command)
    if not ok:
        log(f"job validation failed: {why}")
        sys.exit(1)
    if not isinstance(enabled, bool):
        log("job validation failed: 'enabled' must be a boolean")
        sys.exit(1)
    try:
        parse_duration(expire_at)
    except ValueError as e:
        log(f"job validation failed: 'expire_at': {e}")
        sys.exit(1)
    expire_at = _normalize_duration(expire_at)
    out = {"name": name.strip(), "crons": stripped, "command": command.strip(), "enabled": enabled}
    # Only persist on_overlap/expire_at when non-default, to keep tidy configs tidy.
    if policy != _DEFAULT_OVERLAP:
        out["on_overlap"] = policy
    if expire_at != "never":
        out["expire_at"] = expire_at
    return out


def cmd_put(cfg_path: Path, raw_job: str, yes: bool) -> None:
    data = load_config_data(cfg_path)
    jobs = data["jobs"]
    new_job = parse_put_job(raw_job)

    idx = next((i for i, j in enumerate(jobs) if j.get("name") == new_job["name"]), None)
    if idx is None:
        new_job["created_at"] = _now_str()
        jobs.append(new_job)
        save_config_data(cfg_path, data)
        log(f"added job {new_job['name']!r}")
        notify_scheduler_reload(cfg_path)
        return

    old = jobs[idx]
    # created_at is stamped once at first registration and never reset by an
    # update — an --put replace extends the job's config but must not restart
    # its expire_at clock (mirrors watch-pr's replace-preserves-created_at rule).
    new_job["created_at"] = old.get("created_at") or _now_str()
    old_command = str(old.get("command", ""))
    if old_command != new_job["command"]:
        log(f"job {new_job['name']!r} already exists and command differs")
        log(f"existing command: {old_command}")
        log(f"new command:      {new_job['command']}")
        if not yes:
            if not sys.stdin.isatty():
                log("refusing to update command without confirmation in non-interactive mode (use --yes)")
                sys.exit(1)
            answer = input("continue updating this job? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                log("update cancelled.")
                sys.exit(0)
    jobs[idx] = new_job
    save_config_data(cfg_path, data)
    log(f"updated job {new_job['name']!r}")
    notify_scheduler_reload(cfg_path)


def cmd_delete(cfg_path: Path, name: str) -> None:
    data = load_config_data(cfg_path)
    jobs = data["jobs"]
    before = len(jobs)
    jobs[:] = [j for j in jobs if j.get("name") != name]
    if len(jobs) == before:
        log(f"job {name!r} does not exist; nothing changed")
        return
    save_config_data(cfg_path, data)
    log(f"deleted job {name!r}")
    notify_scheduler_reload(cfg_path)


# ---- execution ------------------------------------------------------------

def start_job(job: dict) -> None:
    """Fire a job's command detached. Skips if a prior run is still active."""
    proc = job.get("proc")
    if proc is not None and proc.poll() is None:
        log(f"[{job['name']}] previous run still active (pid {proc.pid}) — skipping this tick")
        return
    log(f"[{job['name']}] starting: {job['command']}")
    job["proc"] = subprocess.Popen(
        job["command"],
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=None,
        stderr=None,
        start_new_session=True,  # detach from the scheduler's process group
    )


def reap(jobs: list[dict]) -> None:
    """Log any jobs that have finished since the last tick."""
    for job in jobs:
        proc = job.get("proc")
        if proc is not None and proc.poll() is not None:
            rc = proc.returncode
            level = "" if rc == 0 else " (non-zero)"
            log(f"[{job['name']}] finished rc={rc}{level}")
            job["proc"] = None


def _announce_active(active: list[dict]) -> None:
    log(f"scheduler up — {len(active)} enabled job(s): " + ", ".join(j["name"] for j in active))
    for j in active:
        schedules = " | ".join(c.expr for c in j["crons"])
        log(f"  · {j['name']}: {schedules}  ->  {j['command']}")


def _reattach_running(active: list[dict], prev: list[dict]) -> None:
    """Carry live subprocess handles from the previous job set into the freshly
    reloaded one, matched by name. Without this, a reload drops every running
    job's Popen handle (fresh dicts start with proc=None) and the no-overlap
    guard in start_job would launch a second concurrent instance next tick."""
    live = {j["name"]: j["proc"] for j in prev if j.get("proc") is not None}
    for j in active:
        if j["name"] in live:
            j["proc"] = live.pop(j["name"])
    # Jobs that were running but are gone/renamed after the reload: we no longer
    # track them, but they keep running detached. Note it so it isn't a silent leak.
    for name in live:
        log(f"[{name}] was running but is no longer in config after reload — leaving it detached")


def _run_loop_forever(cfg_path: Path) -> None:
    """Blocking tick loop. Runs until SIGTERM / KeyboardInterrupt."""
    active = [j for j in load_jobs(cfg_path) if j["enabled"]]
    _announce_active(active)
    # An empty job list does NOT exit — the scheduler idles and waits for a SIGHUP reload. This
    # matters for two cases: a freshly-seeded config (jobs: []) shouldn't crash-loop under a
    # KeepAlive supervisor, and `--put`/`--delete` (which SIGHUP a *running* scheduler) must have a
    # process to signal even when the config started empty.
    if not active:
        log("no enabled jobs — idling; add one via `--put` (SIGHUP reloads).")

    last_minute = None
    while True:
        global RELOAD_REQUESTED
        if RELOAD_REQUESTED:
            RELOAD_REQUESTED = False
            try:
                reloaded = [j for j in load_jobs(cfg_path) if j["enabled"]]
            except ConfigError as e:
                # Do NOT die on a bad edit — that would crash-loop under launchd
                # KeepAlive on the same broken config. Keep the last-good set.
                log(f"reload failed: {e} — keeping previous {len(active)} job(s)")
            else:
                _reattach_running(reloaded, active)
                active = reloaded
                log("reload requested — reloaded config")
                _announce_active(active)
                if not active:
                    log("no enabled jobs after reload — idling; add one via `--put` (SIGHUP reloads).")
        now = datetime.now()
        this_minute = now.replace(second=0, microsecond=0)
        if this_minute != last_minute:
            last_minute = this_minute
            reap(active)
            still_active = []
            for job in active:
                if job_should_fire(job, now):
                    if job_is_expired(job, now):
                        log(f"[{job['name']}] expired (created_at={job.get('created_at')}, "
                            f"expire_at={job.get('expire_at')}) — auto-deregistering")
                        cmd_delete(cfg_path, job["name"])
                        continue  # drop from active; do not start it
                    start_job(job)
                still_active.append(job)
            active = still_active
        # Sleep to just past the next minute boundary so we tick once per minute.
        sleep_s = 60 - datetime.now().second + 1
        time.sleep(max(1, sleep_s))


def run_loop(cfg_path: Path) -> int:
    """Start the scheduler in a detached background process and return its PID
    immediately. The child re-execs this script with ``run --foreground``."""
    script = Path(sys.argv[0]).resolve()
    cmd = [sys.executable, str(script), "run", "--foreground"]
    proc = subprocess.Popen(
        cmd,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return proc.pid


def _run_foreground(cfg_path: Path) -> None:
    """Own the pidfile and block in the tick loop (used by launchd / the background child)."""
    signal.signal(signal.SIGHUP, _request_reload)
    # SIGTERM is how launchctl bootout / systemctl stop / plain kill stop us.
    # Convert it to SystemExit so the finally-block below unlinks the pidfile;
    # a stale pidfile plus PID reuse would otherwise misdirect a later SIGHUP.
    signal.signal(signal.SIGTERM, _handle_termination)
    pidfile = _write_pidfile(cfg_path)
    try:
        _run_loop_forever(cfg_path)
    except KeyboardInterrupt:
        log("interrupted — shutting down.")
    except SystemExit:
        pass  # from _handle_termination; fall through to pidfile cleanup
    except ConfigError as e:
        # Only reachable from the initial load; the reload path inside the loop
        # recovers on its own. Startup on a bad config still can't proceed.
        log(str(e))
        sys.exit(1)
    finally:
        if pidfile is not None:
            try:
                # Only unlink if the file still names us (first line == our pid),
                # so a same-config scheduler that started after us keeps its file.
                owner = pidfile.read_text().splitlines()[0].strip()
                if owner == str(os.getpid()):
                    pidfile.unlink()
            except (OSError, ValueError, IndexError):
                pass


def cmd_list(jobs: list[dict]) -> None:
    if not jobs:
        log("no valid jobs configured.")
        return
    for job in jobs:
        state = "enabled" if job["enabled"] else "disabled"
        print(f"{job['name']}  [{state}]")
        crons = job["crons"]
        if len(crons) == 1:
            print(f"    crons:   {crons[0].expr}")
        else:
            print("    crons:")
            for c in crons:
                print(f"        - {c.expr}")
        if job.get("on_overlap", _DEFAULT_OVERLAP) != _DEFAULT_OVERLAP:
            print(f"    on_overlap: {job['on_overlap']}")
        if job.get("expire_at", "never") != "never":
            print(f"    expire_at: {job['expire_at']}  (created {job.get('created_at') or 'unknown'})")
        print(f"    command: {job['command']}")


def cmd_test(jobs: list[dict], name: str) -> None:
    match = next((j for j in jobs if j["name"] == name), None)
    if match is None:
        log(f"no job named {name!r}. known: " + ", ".join(j["name"] for j in jobs))
        sys.exit(1)
    log(f"[{name}] test run (foreground): {match['command']}")
    rc = subprocess.call(match["command"], shell=True)
    log(f"[{name}] test finished rc={rc}")
    sys.exit(rc)


def main() -> None:
    ap = argparse.ArgumentParser(description="Tiny cron-style job scheduler.")
    ap.add_argument("action", nargs="?", default="run", choices=["run", "list", "test"])
    ap.add_argument("name", nargs="?", help="job name (for `test`)")
    ap.add_argument(
        "--foreground",
        action="store_true",
        help="run the scheduler loop in this process (default `run` backgrounds and prints the PID)",
    )
    ap.add_argument("--put", help='add/update one job from JSON object text, e.g. \'{"name":"x","crons":["*/5 * * * *"],"command":"echo hi","enabled":true}\'')
    ap.add_argument("--delete", help="delete one job by name")
    ap.add_argument("--yes", action="store_true", help="skip confirmation prompts for --put command updates")
    args = ap.parse_args()

    cfg_path = resolve_config()
    if args.put and args.delete:
        ap.error("use only one of --put or --delete")
    if args.foreground and args.action != "run":
        ap.error("--foreground is only valid with `run`")

    try:
        if args.put:
            cmd_put(cfg_path, args.put, args.yes)
            return
        if args.delete:
            cmd_delete(cfg_path, args.delete)
            return

        if args.action == "list":
            cmd_list(load_jobs(cfg_path))
            return
        if args.action == "test":
            if not args.name:
                ap.error("`test` requires a job name")
            cmd_test(load_jobs(cfg_path), args.name)
            return
    except ConfigError as e:
        log(str(e))
        sys.exit(1)

    # run
    # Dedup guard: refuse to start a second scheduler against the same config.
    # The pidfile is already the source of truth --put/--delete use to find the
    # live scheduler, and it's PID-reuse-safe -- unlike matching `ps aux`/`pgrep -f`
    # output by name, which _pidfile_path's docstring above calls out as unreliable.
    existing_pid = _read_pidfile(cfg_path)
    if existing_pid is not None and existing_pid != os.getpid():
        log(f"job-scheduler is already running for this config (pid {existing_pid}).")
        if args.foreground:
            # Under launchd / the background child there is no TTY — leave the
            # existing process alone rather than prompting.
            log("cancelled -- leaving the running scheduler untouched.")
            sys.exit(0)
        try:
            reply = input("Stop it and restart? [y/N] ").strip().lower()
        except EOFError:
            reply = ""  # no TTY -- default to leaving it alone
        if reply not in ("y", "yes"):
            log("cancelled -- leaving the running scheduler untouched.")
            sys.exit(0)
        # Prefer launchctl when the LaunchAgent owns the process: a bare kill is
        # immediately undone by KeepAlive and leaves you fighting a respawn.
        if _launchd_agent_loaded():
            new_pid = _restart_via_launchd(cfg_path, existing_pid)
            if new_pid is None:
                sys.exit(1)
            print(new_pid, flush=True)
            log(f"scheduler restarted via launchd (pid {new_pid})")
            return
        _stop_manual_scheduler(existing_pid)

    if args.foreground:
        _run_foreground(cfg_path)
        return

    pid = run_loop(cfg_path)
    print(pid, flush=True)
    log(f"scheduler started in background (pid {pid})")


if __name__ == "__main__":
    main()
