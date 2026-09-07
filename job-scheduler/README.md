# job-scheduler

A tiny, dependency-light **cron-style task runner** for a single machine. It reads one YAML
config of jobs, ticks once per minute, and runs every enabled job whose 5-field cron matches the
current local time. Jobs run **detached**; a job still running when its next tick fires is
**skipped** (no overlap). Built for pointing launchd (macOS) or systemd/cron (Linux) at a handful
of local automations — pollers, daily reports, sync scans — without a heavier scheduler.

| File | Role |
|---|---|
| [`job-scheduler.py`](./job-scheduler.py) | The scheduler. Runs the loop, or manages jobs (`list` / `test` / `--put` / `--delete`). |
| [`jobs.yaml.template`](./jobs.yaml.template) | Reference config with worked examples. Copy jobs from here; nothing seeds a live config from it automatically. |

Installed/registered by the repo root's [`install.sh`](../../../install.sh) (copies the script,
seeds the shared config) and [`setup.sh`](../../../setup.sh) (registers the launchd agent) — see
below.

Nothing is machine-specific — all paths derive from `$HOME`, and the launchd interpreter is
auto-detected. The only hard dependency is **PyYAML**.

---

## Setup (macOS)

From the repo root:

```bash
./install.sh   # copies job-scheduler.py -> ~/.fulcrum/bin/job-scheduler, seeds the shared
                # ~/.fulcrum/fulcrum_config.yml from fulcrum_config_template.yml (only if missing)
./setup.sh      # (or ~/.fulcrum/bin/fulcrum-setup once install.sh has run) — writes
                # ~/Library/LaunchAgents/com.fulcrum.job-scheduler.plist with RunAtLoad +
                # KeepAlive and loads it via launchctl bootstrap/kickstart
```

`job_scheduler.jobs_file` (default `~/.fulcrum/logs/job_scheduler/jobs.yaml`) needs no separate
seeding step: job-scheduler creates it itself, starting **empty** (`jobs: []`), the first time it
runs against a missing path — nothing runs until you add a job with `job-scheduler --put` (see
[Usage](#usage)) or by copying an example from [`jobs.yaml.template`](./jobs.yaml.template) and
fixing its `command` path.

launchd's raw stdout/stderr go to `~/.local/share/job-scheduler/scheduler.log`; the scheduler's
own rotating log (via `job_scheduler.log_file` in the shared config, default
`~/.fulcrum/logs/job_scheduler/job_scheduler.log`) captures the same messages structured for
`logging`-based tooling.

Re-running `install.sh` / `setup.sh` is safe and idempotent: they refresh the script and reload the
agent in place, and leave your config untouched.

### Linux

There's no launchd; run the loop under systemd or cron instead:

```bash
install -m 0755 job-scheduler.py ~/.local/bin/job-scheduler
~/.local/bin/job-scheduler run          # foreground loop; wrap in a systemd unit for restart-on-exit
```

---

## Config

One file, a top-level `jobs:` list. Config path resolution (first that applies wins):

1. `--config PATH`
2. `$JOB_SCHEDULER_CONFIG`
3. `job_scheduler.jobs_file` in the shared `~/.fulcrum/fulcrum_config.yml` (default
   `~/.fulcrum/logs/job_scheduler/jobs.yaml` — see `fulcrum_config_template.yml` at the repo root)

```yaml
jobs:
  - name: example-poll            # required, unique
    crons:                        # required — a LIST of 5-field expressions
      - "*/15 * * * *"
    command: /absolute/path/to/poll.sh   # required — use an ABSOLUTE path
    enabled: true                 # optional, default true
```

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Unique. Used by `test <name>` and `--delete`. |
| `crons` | yes | A **list** of 5-field expressions; one entry is fine. Several entries make it a multi-schedule job — see below. |
| `command` | yes | Shell command run via `sh -c`. Use an **absolute** path — cron/launchd don't inherit your interactive PATH. |
| `enabled` | no | Boolean `true`/`false`. `false` keeps a job in config but idle. Default `true`. A non-boolean value (e.g. the string `"false"`) is rejected and the job is treated as **disabled** (fail closed). |
| `on_overlap` | no | Only meaningful with multiple schedules whose time windows overlap. `error` (default), `all`, `densest`, or `sparsest`. See below. |

**Cron fields (5):** `minute hour day-of-month month day-of-week`. Supports `*`, `*/step`, `a-b`
ranges, `a-b/step`, and comma lists (e.g. `7,22,37,52`). `day-of-week` is `0-6` (Sun=0); `7` also
means Sunday. Standard cron day semantics: if **both** day-of-month and day-of-week are restricted,
the job runs when **either** matches; otherwise both must match.

### Multiple schedules per job

`crons` is a **list**, so one job can run on several time ranges — just add more entries. It fires
when the active schedule(s) match, and the no-overlap guard still means at most **one run per
minute** — the command never double-fires:

```yaml
jobs:
  - name: poll
    crons:
      - "*/15 9-17 * * 1-5"   # Mon-Fri business hours, every 15 min
      - "20,50 17-23,0-8 * * *"  # evenings/overnight, at :20 and :50
    on_overlap: densest       # required once windows overlap (see below)
    command: /absolute/path/to/poll.sh
    enabled: true
```

**`on_overlap`** decides what happens when two of a job's schedules have **overlapping time
windows** — the same hour/day/month is active for both (minute aside). It changes **which minutes
fire** at runtime; because a job has one command, it never changes *how many times* it runs (always
at most once per minute).

Worked example: **A** = all-day `*/15 * * * *` (`:00 :15 :30 :45`) plus **B** = evening
`20,50 17-23,0-8 * * *` (`:20 :50`). During the evening, both windows are active:

| Value | Evening firings/hour | Meaning |
|---|---|---|
| `error` (default) | — | The job is **rejected** — `--put` fails; on reload the job is skipped and the last-good set kept. Forces a deliberate choice instead of silently paying for extra runs. |
| `all` | `:00 :15 :20 :30 :45 :50` (6) | Union — fire when **any** active schedule matches. |
| `densest` | `:00 :15 :30 :45` (4) | Only the **densest** active schedule (most firings *per hour*, measured in the overlap window) decides; sparser overlapping ones are muted. |
| `sparsest` | `:20 :50` (2) | Only the **sparsest** active schedule decides. |

Outside the overlap window (e.g. 10am, when only A is active), every policy behaves the same —
only one schedule has a say. A job with a single schedule never overlaps, so the `error` default is
invisible to it.

---

## Usage

```bash
job-scheduler                 # run the scheduler loop (default action is `run`)
job-scheduler list            # print configured jobs and exit
job-scheduler test <name>     # run one job by name immediately in the foreground, then exit
job-scheduler --config PATH   # use a specific config file

# Manage jobs without hand-editing (both hot-reload a running scheduler via SIGHUP):
job-scheduler --put '{"name":"x","crons":["*/5 * * * *"],"command":"echo hi","enabled":true}'
job-scheduler --delete x
```

### Reloading after a manual config edit

The loop reads config at startup and on **SIGHUP**. If you hand-edit `jobs.yaml`, tell the
running scheduler to reload:

```bash
launchctl kickstart -k gui/$(id -u)/com.fulcrum.job-scheduler   # restart the launchd agent
# — or, if you know the pid —
kill -HUP <scheduler-pid>                                    # hot-reload in place
```

`--put` and `--delete` send the SIGHUP for you, so jobs added/removed that way take effect without
a restart. They locate the running scheduler via a pidfile keyed to the config path (under
`$XDG_RUNTIME_DIR` or `~/.local/share/job-scheduler/`), so they signal exactly the scheduler that
owns that config — not other unrelated processes.

---

## Prerequisites

- **`python3` with PyYAML.** The scheduler exits if PyYAML isn't importable. Verify with
  `<python3> -c 'import yaml'`; install with `<python3> -m pip install pyyaml`. On macOS,
  Homebrew's `/opt/homebrew/bin/python3` typically has it — make sure whichever `python3` resolves
  first on PATH (the launchd plist's `EnvironmentVariables.PATH` includes
  `/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin`) has it installed.
- **macOS launchd** for the managed always-on agent (optional — you can run the loop yourself).

---

## Safety properties

- **No overlap** — a job still running when its next tick fires is skipped, with a log line. Live
  job handles are carried across a config reload, so a `--put`/`--delete`/SIGHUP mid-run never
  double-fires a job that's still executing.
- **Survives a bad config** — an invalid config edit doesn't take the scheduler down. On startup a
  broken config is fatal (nothing to fall back to), but a bad **reload** logs the error and keeps
  the last-good job set, so a typo under `KeepAlive` can't turn into a permanent crash-loop.
- **Detached jobs** — each command runs in its own session (`start_new_session=True`), so a job
  outliving a tick or a scheduler restart won't take the scheduler down with it.
- **Restart throttle** — the launchd agent uses `ThrottleInterval` so a crash-looping job can't
  spin the CPU.
- **Config never clobbered** — job-scheduler creates `jobs.yaml` itself, starting empty
  (`jobs: []`), only when the path is missing; an existing file is never touched.
- **One scheduler per config** — `run` refuses to start a second instance against the same
  config: it checks a pidfile keyed to the config path and, if one is already running, asks
  before stopping it and taking over (two live schedulers on one config would double-fire
  every job).
