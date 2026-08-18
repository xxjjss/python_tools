# Sticky Note Widget

A floating always-on-top macOS desktop widget that shows your GUS work items at a glance,
grouped by status and persisted to a daily journal file.

## Prerequisites

| Requirement | Details |
|-------------|---------|
| **macOS** | macOS 11 (Big Sur) or later — Apple Silicon or Intel |
| **Python** | Python 3.10+ |
| **PyObjC** | `pip install pyobjc-framework-Cocoa pyobjc-framework-WebKit` |
| **PyYAML** | `pip install pyyaml` |
| **sf CLI** | Salesforce CLI installed at `~/.aisuite/bin/sf` |
| **GUS org** | `sf` org aliased as `gus` pointing to `https://gus.my.salesforce.com` |

### Install dependencies in one step

```bash
pip install pyobjc-framework-Cocoa pyobjc-framework-WebKit pyyaml
```

## Installation

Run the install script once to copy the widget files and create a `sticky-note` entry point in `~/.local/bin/`:

```bash
bash install_sticky_note.sh
```

This copies all widget files to `~/.local/share/sticky-note/` and creates `~/.local/bin/sticky-note`.

Make sure `~/.local/bin` is on your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"   # add to ~/.zshrc to make permanent
```

## Usage

### Launch the widget

```bash
sticky-note
```

Or run the launch script directly:

```bash
bash launch_sticky_note.sh
```

On **first run** the script will:
1. Check if `sf` is logged in to the `gus` org — opens browser SSO if not.
2. Create `config.yaml` from `config.yaml.template` in the widget directory.
3. Prompt for your GUS email address (saved to `config.yaml` **and** the journal; not prompted again).
4. Create today's journal file at `<journal_path>/YYYY-MM-DD-journal.md`.
5. Start the widget as a detached background process.

On **subsequent runs** the stored email and journal are reused automatically.

## Configuration

Settings live in `config.yaml` next to the widget files (once installed:
`~/.local/share/sticky-note/config.yaml`). It is created from
`config.yaml.template` on first launch, so the defaults and their explaining
comments are preserved. Edit `config.yaml` and re-run `sticky-note` to apply.

| Key | Default | Meaning |
|-----|---------|---------|
| `user` | `""` | GUS email used to query your work items. Empty on a fresh install; filled in after the first launch. |
| `journal_path` | `~/Journal` | Directory holding the daily journal files. `~` is expanded. |
| `refresh_interval_minutes` | `15` | How often the widget re-queries GUS. Positive integer. |
| `agent_poll_interval_minutes` | `5` | How often the widget polls the background agents it launched and folds their output into the journal. Positive integer. |
| `auto_invoke_wi_worker` | `false` | When `true`, the widget auto-launches the `tcm-wi-worker` agent for work items without asking. When `false` (default), agents are only launched on an explicit user action. |
| `socket_port` | `48327` | Loopback TCP port for the external control socket (immediate-refresh signals). Change it if the port collides. Required key — a config missing it fails to start. |
| `agents` | see template | Agents the widget may launch. `default` holds shared settings (`model`, `effort`, `permission-mode`, `output-format`, `max-budget-usd`, `timeout-minutes`); each named entry inherits any key it omits. Launching an agent not listed here is an error. |

If `config.yaml`'s `user` and the email recorded in the journal are both set
but **disagree**, the widget refuses to start — this prevents mixing two users'
data. To switch users, update `user` and point `journal_path` at the matching
journal directory.

### Add a Non-WI task from outside the widget

Add an "Others" task without opening the widget UI — handy for scripts, aliases,
or other agents. Takes a required title and an optional comment (the same
comment shown on hover / edited by double-click in the panel):

```bash
sticky-note-add "Review PR #123"
sticky-note-add "Review PR #123" "detail: https://github.com/org/repo/pull/123"
id=$(sticky-note-add "Review PR #123" --id-only)   # capture the task's UUID
```

The task is appended to today's journal (created if needed). If the widget is
running it is signalled to refresh immediately (see below), so the task appears
at once; otherwise it shows on the next auto-refresh.

It prints the new task's **UUID** — the widget's stable key for that task (used
for carry-forward, comments, and removal). Pass `--id-only` to print just the
UUID on stdout (all other messages go to stderr) so a script can capture it
cleanly. Exit codes: `0` added, `2` missing title, `3` config error.

### Manage Non-WI tasks as an API (`sticky-note-task`)

`sticky-note-task` is a CRUD API over the Non-WI tasks for scripts and other
agents. Every mutation writes the journal (under a cross-process lock, mirroring
the widget) and then signals a running widget to refresh:

```bash
# put — add a task; prints the new UUID
id=$(sticky-note-task put '{"title":"Review PR #123","comment":"detail: ..."}')

# get — print the task as JSON
sticky-note-task get "$id"
# {"uuid":"...","title":"Review PR #123","comment":"detail: ...","checked":false}

# get with no UUID — print every Non-WI task as a JSON array (journal order)
sticky-note-task get
# [{"uuid":"...","title":"...","comment":"...","checked":false}, ...]

# update — change only the fields you pass (id required); prints the task
sticky-note-task update "{\"id\":\"$id\",\"checked\":true}"
sticky-note-task update "{\"id\":\"$id\",\"title\":\"New title\",\"comment\":\"\"}"

# delete — remove the task (tombstoned so carry-forward can't resurrect it)
sticky-note-task delete "$id"
# {"deleted":true,"id":"..."}
```

- **`get` with no UUID lists everything** — a JSON array of all Non-WI tasks in
  journal order (empty array `[]` if there are none).
- **Bodies are JSON strings.** `put`: `{"title":<required>, "comment":<optional>}`.
  `update`: `{"id":<required-uuid>, "title"?, "comment"?, "checked"?}` — any
  field you omit is left unchanged. (`comments` is accepted as an alias for
  `comment` on input.)
- **`checked`** maps to the task's completed state (`true` = checked off).
- Exit codes: `0` success · `2` usage / bad-JSON / validation error · `3` config
  error · `4` task not found. Errors print to stderr; on success the JSON result
  is the only thing on stdout.

(`sticky-note-add` is a thin convenience wrapper for the common add case;
`sticky-note-task put` is the full-fidelity equivalent.)

### Refresh the widget immediately from outside

The running widget listens on a **loopback TCP control socket**
(`127.0.0.1:<socket_port>`, default port `48327`, set by `socket_port` in
`config.yaml`). Send it a "refresh now" signal so an external change is picked
up at once instead of waiting for the next auto-refresh tick:

```bash
sticky-note-refresh                 # reload the local journal + re-render (cheap)
sticky-note-refresh --scope gus     # force a full GUS re-query
```

The wire protocol is one line of JSON (`{"cmd":"refresh","scope":"journal"}`),
so it's easy to extend with new commands and portable off macOS. The socket is
bound to loopback only — never reachable off the machine. `sticky-note-add`
sends this signal automatically after adding a task. Exit codes: `0` delivered,
`1` widget not running / transport error, `3` config error.

If the configured `socket_port` is already in use, the widget prints the
conflict and exits at startup — change `socket_port` in `config.yaml` and
relaunch.

### Refresh now from the widget (⚡)

The header shows a ⚡ lightning bolt just before the `Last refresh:` time.
**Double-click it** to force an immediate GUS re-query without waiting for the
next auto-refresh tick. Doing so also **resets the 15-minute clock** — the next
scheduled refresh is a full interval away, not moments later. The bolt dims
while the refresh is in flight and clears when fresh data renders.

### Stop the widget

```bash
pkill -f sticky_note.py
```

### Re-open after closing

Just run `sticky-note` (or `bash launch_sticky_note.sh`) again.

## Main Features

| Feature | Description |
|---------|-------------|
| **Four sections** | **In Progress** · **Done** · **TODO** (current sprint) · **Others** (free-form tasks) |
| **Always-on-top** | Floating NSPanel — stays visible when switching apps |
| **Menu-bar toggle** | 📌 icon in the menu bar shows/hides the panel |
| **Color coding** | Deep blue `#1565C0` = entered the section < 2 h ago; dark red `#B71C1C` = Waiting or overdue TODO |
| **Auto-refresh** | Queries GUS every 15 minutes; re-login triggered automatically if the session expires |
| **Refresh now (⚡)** | Double-click the ⚡ bolt in the header to re-query GUS immediately; resets the 15-minute auto-refresh clock |
| **Others tasks** | Add free-form tasks, check them off, remove them via the widget UI |
| **Daily journal** | Every state change is written atomically to `~/Journal/YYYY-MM-DD-journal.md` |
| **Day rollover** | At midnight, a new journal is created; incomplete Others tasks carry forward |
| **Draggable** | Drag the panel by its header to reposition |
| **Invoke agent on a WI** | **Double-click a TODO or In Progress WI's subject text** (not the W-number) to pick a configured agent and run it on that WI; clicking the W-number still opens GUS. Done rows are inert. Set `auto_invoke_wi_worker: true` to launch `tcm-wi-worker` automatically as WIs enter the current sprint. |

### GUS work item statuses and their buckets

| Status | Bucket |
|--------|--------|
| New, Triaged | **TODO** |
| Closed, Pending Release, Duplicate, Never, Not a Bug, Not Reproducible (last 24 h) | **Done** |
| Anything else (In Progress, Ready for Review, Fixed, QA In Progress, Waiting, …) | **In Progress** |

## File Reference

| File | Purpose |
|------|---------|
| `sticky_note.py` | AppKit NSPanel + WKWebView window; routes JS messages to journal mutations |
| `sticky_note_gus.py` | SOQL queries and GUS record classification logic |
| `sticky_note_journal.py` | Journal read/write (`~/Journal/YYYY-MM-DD-journal.md`) |
| `sticky_note_add_task.py` | Standalone CLI to append a Non-WI task (`sticky-note-add`); reuses the config + journal modules |
| `sticky_note_task.py` | CRUD API over Non-WI tasks (`sticky-note-task put/get/update/delete`); JSON bodies, signals a refresh after each mutation |
| `sticky_note_control.py` | Loopback TCP control-socket server + client (JSON-lines protocol) for external "refresh now" signals |
| `sticky_note_refresh_cli.py` | Standalone CLI (`sticky-note-refresh`) that signals the running widget to refresh immediately |
| `sticky_note_refresh.py` | 15-minute refresh loop and AppKit timer setup |
| `sticky_note_config.py` | Loads `config.yaml` (user / journal_path / refresh + agent-poll intervals / agents); user-conflict check |
| `sticky_note_agents.py` | Launches background agents (`claude --agent …`) and polls their runs, folding output into the journal per-WI |
| `sticky_note_template.html` | HTML/CSS/JS template rendered inside the WKWebView |
| `config.yaml.template` | Commented defaults; copied to `config.yaml` on first launch |
| `launch_sticky_note.sh` | Entry-point script: auth check, config + email bootstrap, journal init, widget start |
| `install_sticky_note.sh` | One-time installer: copies files to `~/.local/share/sticky-note/` |

## Output Locations

| Artifact | Path |
|----------|------|
| Daily journal | `~/Journal/YYYY-MM-DD-journal.md` |
| Widget stdout | `/tmp/sticky_note_stdout.txt` |
| Widget stderr | `/tmp/sticky_note_stderr.txt` |
| GUS query cache | `/tmp/sticky_note_data.json` |

## Troubleshooting

**Widget doesn't start**

```bash
cat /tmp/sticky_note_stderr.txt
```

**GUS session expired during use** — the widget opens a browser re-login window non-blocking and continues showing cached data until the refresh succeeds.

**No items shown** — verify `sf` is logged in:

```bash
~/.aisuite/bin/sf org display --target-org gus
```

**Running on a non-macOS machine** — `sticky_note.py` imports `AppKit` which is macOS-only. The widget is not supported on Linux or Windows.
