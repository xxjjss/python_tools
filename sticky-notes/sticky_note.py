"""AppKit NSPanel + WKWebView window for the sticky-note widget.

Creates a floating always-on-top panel rendering sticky_note_template.html
with injected STICKER_DATA JSON. Routes JS messages back to journal mutations.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import objc
import AppKit
import Foundation
import WebKit

from sticky_note_journal import (
    JournalData, OthersTask, WiHistory,
    diff_and_update, save_journal, load_journal, bootstrap_journal,
)
from sticky_note_gus import GusData, is_overdue, is_current_sprint, is_waiting
from sticky_note_agents import (
    icon_for_status as _icon_for_status,
    resolve_wi_agent_status as _resolve_wi_agent_status,
    wi_agent_status_lines as _wi_agent_status_lines,
    wi_agent_details as _wi_agent_details,
)
from sticky_note_wi_agents import spawn_wi_agent, auto_invoke_wi_worker

TEMPLATE_PATH = Path(__file__).parent / "sticky_note_template.html"

# Icons are inlined as base64 data URIs rather than referenced by relative path.
# WKWebView's loadHTMLString:baseURL: does not grant a file:// base URL read
# access to sibling files, so <img src="resource/*.png"> renders broken; a
# data: URI needs no file access. The base64 is pre-computed once at install
# time (install_sticky_note.sh writes a sibling `<png>.b64` holding the full
# data URI); here we just read that file. Cache the read per relative path.
_ICON_DATA_URI_CACHE: dict = {}


def _icon_data_uri(rel_path: "str | None") -> "str | None":
    """Return the pre-encoded base64 data URI for a widget-relative icon path
    (e.g. 'resource/agent-running.png'), read from its sibling `<path>.b64`
    file. Returns None if the path is falsy or the .b64 file is missing."""
    if not rel_path:
        return None
    if rel_path in _ICON_DATA_URI_CACHE:
        return _ICON_DATA_URI_CACHE[rel_path]
    try:
        uri = (TEMPLATE_PATH.parent / (rel_path + ".b64")).read_text(
            encoding="ascii"
        ).strip()
    except OSError as exc:
        print(f"[sticky-note] icon {rel_path}.b64 unreadable "
              f"(run install_sticky_note.sh): {exc}", file=sys.stderr)
        uri = None
    _ICON_DATA_URI_CACHE[rel_path] = uri
    return uri
MIN_HEIGHT = 32
DEFAULT_WIDTH = 360
VERSION = "2.2"
SF_BIN = str(Path.home() / ".aisuite/bin/sf")
GUS_ALIAS = "gus"


# Serializes every journal load -> mutate -> save sequence so the UI thread
# (checkbox_toggle / add_task / remove_tasks_dialog) and the background refresh
# thread (_apply_gus_refresh) can't interleave and lose each other's updates.
_JOURNAL_LOCK = threading.Lock()


_AUTH_ERROR_MARKERS = ("INVALID_SESSION_ID", "expired access/refresh token",
                       "invalid_grant", "not logged in", "No AuthInfo found")


def _is_auth_error(message: str) -> bool:
    msg = (message or "").lower()
    return any(m.lower() in msg for m in _AUTH_ERROR_MARKERS)


def _trigger_gus_relogin() -> None:
    """Open browser for GUS SSO re-login without blocking the main thread."""
    print("[sticky-note] GUS token expired — opening browser for re-login", file=sys.stderr)
    subprocess.Popen(
        [SF_BIN, "org", "login", "web",
         "--alias", GUS_ALIAS,
         "--instance-url", "https://gus.my.salesforce.com"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def refresh_gus_from_sf(user_email: str, journal_path: str) -> "tuple[GusData, dict[str, str]] | None":
    """Query GUS via `sf data query` and update /tmp/sticky_note_data.json.
    Returns (GusData, done_streak_times) on success, None on failure.
    done_streak_times: {wi_name -> ISO UTC timestamp of first entry into current done streak}
    """
    from sticky_note_gus import QUERY_A, QUERY_B, build_gus_data

    def run_query(soql: str) -> "list[dict] | None":
        from datetime import timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        q = (soql
             .replace("CURRENT_USER_EMAIL", user_email)
             .replace("LAST_N_HOURS:24", cutoff))
        try:
            result = subprocess.run(
                [SF_BIN, "data", "query", "--query", q,
                 "--target-org", GUS_ALIAS, "--json"],
                capture_output=True, text=True, timeout=30,
            )
            data = json.loads(result.stdout)
            if data.get("status") != 0:
                msg = data.get("message", "")
                print(f"[sticky-note] sf query error: {msg}", file=sys.stderr)
                if _is_auth_error(msg) or _is_auth_error(data.get("name", "")):
                    _trigger_gus_relogin()
                return None
            return data["result"]["records"]
        except Exception as exc:
            print(f"[sticky-note] sf query failed: {exc}", file=sys.stderr)
            return None

    records_a = run_query(QUERY_A)
    records_b = run_query(QUERY_B)
    if records_a is None or records_b is None:
        return None

    gus = build_gus_data(records_a, records_b, date.today())

    # Query ADM_Work__History to get the actual done-streak start time for each done WI
    done_streak_times: dict[str, str] = {}
    done_names = [wi.name for wi in gus.done]
    if done_names:
        names_csv = ", ".join(f"'{n}'" for n in done_names)
        history_soql = (
            f"SELECT Parent.Name, CreatedDate, OldValue, NewValue "
            f"FROM ADM_Work__History "
            f"WHERE Field = 'Status__c' "
            f"AND Parent.Name IN ({names_csv}) "
            f"ORDER BY CreatedDate ASC"
        )
        try:
            result = subprocess.run(
                [SF_BIN, "data", "query", "--query", history_soql,
                 "--target-org", GUS_ALIAS, "--json"],
                capture_output=True, text=True, timeout=30,
            )
            hdata = json.loads(result.stdout)
            if hdata.get("status") == 0:
                # Build per-WI history list, then find streak start
                from collections import defaultdict
                wi_history: dict[str, list[dict]] = defaultdict(list)
                _done_statuses = {
                    "Closed", "Pending Release",
                    "Duplicate", "Never", "Not a Bug", "Not Reproducible",
                }
                for rec in hdata["result"]["records"]:
                    wi_name = rec.get("Parent", {}).get("Name", "")
                    if wi_name:
                        wi_history[wi_name].append(rec)
                for wi_name, events in wi_history.items():
                    # events already sorted ASC by query
                    # Walk backwards to find start of last contiguous done streak
                    streak_start_ts = None
                    for ev in reversed(events):
                        if ev.get("NewValue") in _done_statuses:
                            streak_start_ts = ev["CreatedDate"]
                        elif ev.get("OldValue") not in _done_statuses:
                            # non-done → non-done transition, stop
                            break
                        else:
                            # done → non-done: streak broken, stop
                            break
                    if streak_start_ts:
                        done_streak_times[wi_name] = streak_start_ts
        except Exception as exc:
            print(f"[sticky-note] history query failed: {exc}", file=sys.stderr)

    try:
        with open("/tmp/sticky_note_data.json", "w", encoding="utf-8") as f:
            json.dump({
                "query_a": records_a,
                "query_b": records_b,
                "journal_path": journal_path,
                "done_streak_times": done_streak_times,
            }, f)
    except Exception:
        pass

    return gus, done_streak_times


# ---------------------------------------------------------------------------
# build_sticker_data — pre-computes all boolean flags for the HTML template
# ---------------------------------------------------------------------------

def filter_stale_done(gus: GusData, done_streak_times: "dict[str, str]", now: datetime) -> GusData:
    """Remove done items whose actual done-streak start (from ADM_Work__History) is > 24h ago.

    done_streak_times: {wi_name -> ISO UTC timestamp string} from refresh_gus_from_sf.
    WIs with no history entry are kept (streak time unknown).
    """
    from datetime import timezone
    cutoff_ts = now.timestamp() - 86400

    def _within_24h(wi_name: str) -> bool:
        ts_str = done_streak_times.get(wi_name)
        if not ts_str:
            return True  # no history data — keep
        try:
            # CreatedDate from GUS is UTC with Z suffix
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return ts.timestamp() > cutoff_ts
        except ValueError:
            return True

    return GusData(
        in_progress=gus.in_progress,
        done=[wi for wi in gus.done if _within_24h(wi.name)],
        todo_current=gus.todo_current,
        todo_other=gus.todo_other,
    )


def build_sticker_data(
    gus: GusData,
    journal: JournalData,
    now: datetime,
    collapsed: bool = True,
    panel_collapsed: bool = False,
) -> dict:
    today = now.date()
    two_hours_ago = now.timestamp() - 7200

    def recently_entered(wi_name: str) -> bool:
        state = journal.wi_state.get(wi_name)
        if not state or not state.history:
            return False
        try:
            ts = datetime.fromisoformat(state.history[-1].entered_at).timestamp()
            return ts >= two_hours_ago
        except ValueError:
            return False

    def agent_icon(wi_name: str) -> "str | None":
        """Icon path for the WI's reduced agent status (running/done/…), or None."""
        state = journal.wi_state.get(wi_name)
        if not state:
            return None
        status = _resolve_wi_agent_status(getattr(state, "agent_state", None))
        return _icon_data_uri(_icon_for_status(status))

    def agent_status_lines(wi_name: str) -> "list[str]":
        """Per-agent hover-tooltip lines (running first, then newest-started
        first), e.g. ['tcm-wi-worker is running', 'tcm-wi-researcher succeeded']."""
        state = journal.wi_state.get(wi_name)
        if not state:
            return []
        return _wi_agent_status_lines(getattr(state, "agent_state", None))

    def agent_details(wi_name: str) -> "list[dict]":
        """Per-agent detail records for the double-click read-only panel."""
        state = journal.wi_state.get(wi_name)
        if not state:
            return []
        return _wi_agent_details(getattr(state, "agent_state", None))

    def wi_dict(wi, section: str) -> dict:
        waiting = section == "in_progress" and is_waiting(wi)
        overdue = section == "todo" and is_overdue(wi, today)
        return {
            "id": wi.id,
            "name": wi.name,
            "subject": wi.subject,
            "status": wi.status,
            "is_waiting": waiting,
            "is_overdue": overdue,
            # Waiting and overdue items never get deep-blue
            "recently_entered": False if (waiting or overdue) else recently_entered(wi.name),
            # Agent status icon (relative to the widget dir), or None if the WI
            # has no agents attached. failed/timed_out share the failed icon.
            "agent_icon": agent_icon(wi.name),
            # Per-agent status lines for the icon hover tooltip (empty if none).
            "agent_status_lines": agent_status_lines(wi.name),
            # Full per-agent detail records for the double-click read-only panel.
            "agent_details": agent_details(wi.name),
        }

    def _wi_number(name: str) -> int:
        """Extract numeric part of W-XXXXXXX for descending sort."""
        try:
            return int(name.split("-", 1)[1])
        except (IndexError, ValueError):
            return 0

    def sort_section(items: list[dict]) -> list[dict]:
        """Blue first, red second, rest by WI number descending."""
        return sorted(items, key=lambda w: (
            0 if w["recently_entered"] else (1 if (w["is_waiting"] or w["is_overdue"]) else 2),
            -_wi_number(w["name"]),
        ))

    def others_list() -> list[dict]:
        now_ts = now.timestamp()
        result = []
        for i, task in enumerate(journal.others):
            completed = task.completed is not None
            recently_done = False
            if completed and task.completed:
                try:
                    ct = datetime.fromisoformat(task.completed).timestamp()
                    recently_done = (now_ts - ct) < 86400  # within 24h
                except ValueError:
                    pass
            # Hide tasks completed more than 24h ago
            if completed and not recently_done:
                continue
            result.append({
                "index": i,
                "text": task.text,
                "completed": completed,
                "recently_completed": recently_done,
                "comment": task.comment,
            })
        return result

    return {
        "in_progress":        sort_section([wi_dict(w, "in_progress") for w in gus.in_progress]),
        "done":               sort_section([wi_dict(w, "done") for w in gus.done]),
        "todo_current":       sort_section([wi_dict(w, "todo") for w in gus.todo_current]),
        "todo_other":         sort_section([wi_dict(w, "todo") for w in gus.todo_other]),
        "others":             others_list(),
        "todo_other_collapsed": collapsed,
        "panel_collapsed":    panel_collapsed,
        "last_refresh":       now.strftime("%Y-%m-%dT%H:%M:%S"),
        "version":            VERSION,
    }


# ---------------------------------------------------------------------------
# StickerMessageHandler
# ---------------------------------------------------------------------------

class StickyNoteMessageHandler(AppKit.NSObject):
    """WKScriptMessageHandler — routes JS messages to journal mutations."""

    def initWithPanel_journalPath_(self, panel, journal_path: str):
        self = objc.super(StickyNoteMessageHandler, self).init()
        if self is None:
            return None
        self._panel = panel
        self._panel._journal_path = journal_path
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        try:
            body = message.body()
            if isinstance(body, str):
                body = json.loads(body)
            action = body.get("action", "")
            payload = body.get("payload", {})
            self._dispatch(action, payload)
        except Exception as exc:
            print(f"[sticky-note] message handler error: {exc}", file=sys.stderr)

    def radioChanged_(self, sender):
        """Action for the invoke-dialog radio buttons. NSButtons of radio type
        only form a mutually-exclusive group when they share the SAME target +
        action selector (a common superview is not enough on modern AppKit);
        AppKit then clears the others in the group when one fires. This handler
        needs no body — wiring every radio to it is what enforces exclusivity."""
        pass

    @objc.python_method
    def _dispatch(self, action: str, payload: dict):
        if action == "resize":
            self._panel.auto_resize(int(payload.get("height", MIN_HEIGHT)))
            return
        if action == "open_link":
            url_str = payload.get("url", "")
            if url_str:
                ns_url = Foundation.NSURL.URLWithString_(url_str)
                AppKit.NSWorkspace.sharedWorkspace().openURL_(ns_url)
            return
        if action == "collapse_toggle":
            self._panel._collapsed = not payload.get("expanded", True)
            return
        if action == "panel_collapse_toggle":
            self._panel._panel_collapsed = bool(payload.get("collapsed", False))
            return
        if action == "request_key_window":
            self._panel.makeKeyWindow()
            return
        if action == "refresh_now":
            # Lightning-bolt double-click: force an immediate GUS re-query and
            # push the auto-refresh timer's next fire out a full interval, so a
            # manual refresh effectively restarts the 15-minute clock. Wired up
            # in __main__ (needs the module-level timer); no-op if unset.
            cb = getattr(self._panel, "_on_refresh_now", None)
            if cb is not None:
                cb()
            return
        if action == "move_by":
            dx = int(payload.get("dx", 0))
            dy = int(payload.get("dy", 0))
            if dx or dy:
                frame = self._panel.frame()
                # macOS Y axis is flipped vs screen coords: drag down = negative dy
                new_frame = AppKit.NSMakeRect(
                    frame.origin.x + dx,
                    frame.origin.y - dy,
                    frame.size.width,
                    frame.size.height,
                )
                self._panel.setFrameOrigin_(new_frame.origin)
            return

        # Mutations that require journal reload + save + re-render.
        # Hold _JOURNAL_LOCK across the whole load -> mutate -> save so the
        # background refresh thread can't read a stale journal and clobber us.
        now = datetime.now()

        if action in ("checkbox_toggle", "add_task", "remove_tasks"):
            with _JOURNAL_LOCK:
                journal = load_journal(self._panel._journal_path)
                changed = False

                if action == "checkbox_toggle":
                    idx = payload.get("index")
                    if idx is not None:
                        idx = int(idx)
                    if idx is not None and 0 <= idx < len(journal.others):
                        task = journal.others[idx]
                        if task.completed is None:
                            task.completed = now.strftime("%Y-%m-%dT%H:%M:%S")
                        else:
                            task.completed = None
                        changed = True

                elif action == "add_task":
                    text = (payload.get("text") or "").strip()
                    if text:
                        journal.others.append(OthersTask(
                            text=text,
                            added=now.strftime("%Y-%m-%dT%H:%M:%S"),
                            id=uuid.uuid4().hex,
                        ))
                        changed = True

                elif action == "remove_tasks":
                    indices = {int(x) for x in (payload.get("indices") or [])}
                    if indices:
                        # Tombstone removed tasks so carry-forward can't resurrect them.
                        for i, t in enumerate(journal.others):
                            if i in indices:
                                journal.removed_others.add(t.key)
                        journal.others = [
                            t for i, t in enumerate(journal.others) if i not in indices
                        ]
                        changed = True

                if changed:
                    save_journal(self._panel._journal_path, journal)
            if changed:
                self._panel.reload_content(journal)
            return

        if action == "remove_tasks_dialog":
            tasks = payload.get("tasks") or []
            if not tasks:
                return
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("Remove Non-WI Tasks")
            alert.setInformativeText_("Select tasks to remove:")
            alert.addButtonWithTitle_("Remove")
            alert.addButtonWithTitle_("Cancel")
            alert.setAlertStyle_(AppKit.NSAlertStyleInformational)

            # Stack checkboxes as accessory view
            row_h, row_w = 20, 300
            view = AppKit.NSView.alloc().initWithFrame_(
                AppKit.NSMakeRect(0, 0, row_w, row_h * len(tasks))
            )
            checkboxes = []
            for i, task in enumerate(reversed(tasks)):
                y = i * row_h
                cb = AppKit.NSButton.alloc().initWithFrame_(
                    AppKit.NSMakeRect(0, y, row_w, row_h)
                )
                cb.setButtonType_(AppKit.NSSwitchButton)
                cb.setTitle_(task["text"][:60])
                cb.setState_(AppKit.NSOffState)
                cb.setTag_(task["index"])
                view.addSubview_(cb)
                checkboxes.append(cb)
            alert.setAccessoryView_(view)

            self._panel.makeKeyWindow()
            response = alert.runModal()
            if response == AppKit.NSAlertFirstButtonReturn:
                to_remove = {int(cb.tag()) for cb in checkboxes if cb.state() == AppKit.NSOnState}
                if to_remove:
                    with _JOURNAL_LOCK:
                        journal = load_journal(self._panel._journal_path)
                        # Tombstone removed tasks so carry-forward can't resurrect them.
                        for i, t in enumerate(journal.others):
                            if i in to_remove:
                                journal.removed_others.add(t.key)
                        journal.others = [
                            t for i, t in enumerate(journal.others) if i not in to_remove
                        ]
                        save_journal(self._panel._journal_path, journal)
                    self._panel.reload_content(journal)
            return

        if action == "invoke_agent_dialog":
            self._invoke_agent_dialog(payload)
            return

        if action == "edit_comment_dialog":
            self._edit_comment_dialog(payload)
            return

    @objc.python_method
    def _invoke_agent_dialog(self, payload: dict):
        """Double-click on a TODO WI name -> pick an agent and invoke it.

        Shows a small modal: 'Invoke agent on this WI' with the agent listing
        from config (every named entry except `default`) as radio buttons, plus
        Confirm / Cancel. Cancel does nothing; Confirm spawns the chosen agent on
        the WI using spawn_wi_agent (same guard/marker logic as auto-invoke).
        """
        import sticky_note_config as cfgmod
        wi_name = (payload.get("name") or "").strip()
        wi_id = (payload.get("id") or "").strip()
        if not wi_name or not wi_id:
            return

        try:
            cfg = cfgmod.load_config()
        except cfgmod.ConfigError as exc:
            print(f"[sticky-note] invoke dialog: config error: {exc}", file=sys.stderr)
            return
        agent_names = [n for n in cfg.agents.keys() if n != "default"]
        if not agent_names:
            print("[sticky-note] invoke dialog: no agents defined in config",
                  file=sys.stderr)
            return

        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Invoke agent on this WI")
        alert.setInformativeText_(f"{wi_name} — choose an agent to run:")
        alert.addButtonWithTitle_("Confirm")
        alert.addButtonWithTitle_("Cancel")
        alert.setAlertStyle_(AppKit.NSAlertStyleInformational)

        # Radio-button list. NSButtons of radio type become a mutually-exclusive
        # group only when they share the SAME target + action selector — sharing
        # a superview is NOT enough on modern AppKit (without this, every radio
        # toggles independently and several can read as "on"). Wire them all to
        # self.radioChanged_. First option selected by default.
        row_h, row_w = 22, 300
        view = AppKit.NSView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, row_w, row_h * len(agent_names))
        )
        radios = []
        # Lay out top-to-bottom: first agent at the top row.
        for i, name in enumerate(agent_names):
            y = (len(agent_names) - 1 - i) * row_h
            rb = AppKit.NSButton.alloc().initWithFrame_(
                AppKit.NSMakeRect(0, y, row_w, row_h)
            )
            rb.setButtonType_(AppKit.NSRadioButton)
            rb.setTitle_(name)
            rb.setTarget_(self)
            rb.setAction_("radioChanged:")
            rb.setState_(AppKit.NSOnState if i == 0 else AppKit.NSOffState)
            view.addSubview_(rb)
            radios.append(rb)
        alert.setAccessoryView_(view)

        self._panel.makeKeyWindow()
        response = alert.runModal()
        if response != AppKit.NSAlertFirstButtonReturn:
            return  # Cancel

        # rb.title() is an ObjC string (str subclass); coerce to a native str so
        # it can be a plain YAML mapping key. Otherwise yaml.dump tags it with
        # !!python/object/apply:builtins.str and safe_load rejects it on reload.
        chosen = next(
            (str(rb.title()) for rb in radios if rb.state() == AppKit.NSOnState),
            None,
        )
        if not chosen:
            return

        changed = False
        with _JOURNAL_LOCK:
            journal = load_journal(self._panel._journal_path)
            state = journal.wi_state.get(wi_name)
            if state is None:
                print(f"[sticky-note] invoke dialog: {wi_name} not in journal; skipping",
                      file=sys.stderr)
            elif spawn_wi_agent(state, chosen, wi_name, wi_id):
                save_journal(self._panel._journal_path, journal)
                changed = True
        if changed:
            self._panel.reload_content(journal)

    @objc.python_method
    def _edit_comment_dialog(self, payload: dict):
        """Double-click a Non-WI task's text -> edit its optional comment.

        Shows a modal with a multi-line text editor pre-filled with the task's
        current comment (empty if none). Confirm saves the edited text (empty
        clears the comment); Cancel discards. `index` is the position into
        journal.others (same index checkbox_toggle uses), resolved under the
        lock so the background refresh can't shift it out from under us.
        """
        idx = payload.get("index")
        if idx is None:
            return
        idx = int(idx)
        current = str(payload.get("comment") or "")

        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Task comment")
        preview = str(payload.get("text") or "").strip()
        alert.setInformativeText_(preview[:80] if preview else "Edit comment:")
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")
        alert.setAlertStyle_(AppKit.NSAlertStyleInformational)

        # Multi-line editable text area (NSTextView inside a bordered scroll view).
        w, h = 320, 110
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, w, h)
        )
        scroll.setBorderType_(AppKit.NSBezelBorder)
        scroll.setHasVerticalScroller_(True)
        text_view = AppKit.NSTextView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, w, h)
        )
        text_view.setString_(current)
        text_view.setFont_(AppKit.NSFont.systemFontOfSize_(12))
        text_view.setRichText_(False)
        text_view.setAutomaticQuoteSubstitutionEnabled_(False)
        scroll.setDocumentView_(text_view)
        alert.setAccessoryView_(scroll)

        self._panel.makeKeyWindow()
        # Focus the editor so the user can type immediately.
        alert.window().setInitialFirstResponder_(text_view)
        response = alert.runModal()
        if response != AppKit.NSAlertFirstButtonReturn:
            return  # Cancel

        new_comment = str(text_view.string() or "").strip()

        changed = False
        with _JOURNAL_LOCK:
            journal = load_journal(self._panel._journal_path)
            if 0 <= idx < len(journal.others):
                task = journal.others[idx]
                if task.comment != new_comment:
                    task.comment = new_comment
                    save_journal(self._panel._journal_path, journal)
                    changed = True
        if changed:
            self._panel.reload_content(journal)


# ---------------------------------------------------------------------------
# StickerPanel
# ---------------------------------------------------------------------------

class StickyNotePanel(AppKit.NSPanel):
    """Borderless always-on-top floating panel with WKWebView content."""

    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True

    @objc.python_method
    def load_content(self, gus: GusData, journal: JournalData, now: datetime) -> None:
        """Build STICKER_DATA, inject into template, load into WKWebView."""
        data = build_sticker_data(
            gus, journal, now, self._collapsed,
            getattr(self, "_panel_collapsed", False),
        )
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        script_tag = f"<script>window.STICKER_DATA = {json.dumps(data)};</script>\n"
        html = script_tag + template
        base_url = Foundation.NSURL.fileURLWithPath_(str(TEMPLATE_PATH.parent))
        self._webview.loadHTMLString_baseURL_(html, base_url)

    @objc.python_method
    def reload_content(self, journal: JournalData) -> None:
        """Re-render with fresh journal data (called after user interaction)."""
        self.load_content(self._gus, journal, datetime.now())

    @objc.python_method
    def auto_resize(self, height: int) -> None:
        """Resize NSPanel to fit content height reported by JS, growing downward."""
        frame = self.frame()
        new_height = max(height, MIN_HEIGHT)
        delta = new_height - frame.size.height
        if abs(delta) < 2:
            return
        new_frame = AppKit.NSMakeRect(
            frame.origin.x,
            frame.origin.y - delta,
            frame.size.width,
            new_height,
        )
        self.setFrame_display_animate_(new_frame, True, False)

    def windowDidResize_(self, notification):
        """Keep webview filling the panel after a user-driven resize.
        Do NOT call notifyResize — that would snap the panel back to content height."""
        self._webview.setFrame_(self.contentView().bounds())


def _install_edit_menu(app) -> None:
    """Give the app a standard Edit menu so the clipboard/select shortcuts work.

    AppKit only dispatches key equivalents (⌘X/⌘C/⌘V/⌘A) if a menu item
    declares them; a borderless-panel app has no menu at all, so paste into the
    comment editor and copy from the agent-details view silently do nothing.
    Each item targets nil (setAction_ with no setTarget_), letting AppKit send
    the standard cut:/copy:/paste:/selectAll: selectors up the responder chain
    to the focused text field or web view."""
    main_menu = AppKit.NSMenu.alloc().init()

    edit_item = AppKit.NSMenuItem.alloc().init()
    main_menu.addItem_(edit_item)

    edit_menu = AppKit.NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in (
        ("Cut", "cut:", "x"),
        ("Copy", "copy:", "c"),
        ("Paste", "paste:", "v"),
        ("Select All", "selectAll:", "a"),
    ):
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, action, key
        )
        edit_menu.addItem_(item)
    edit_item.setSubmenu_(edit_menu)

    app.setMainMenu_(main_menu)


# ---------------------------------------------------------------------------
# Module-level factory (classmethod can't take Python-typed args in PyObjC)
# ---------------------------------------------------------------------------

def create_sticky_note_panel(
    gus: GusData,
    journal: JournalData,
    journal_path: str,
    now: datetime,
) -> StickyNotePanel:
    """Create, configure, and show the StickyNotePanel."""
    style = (
        AppKit.NSBorderlessWindowMask
        | AppKit.NSMiniaturizableWindowMask
        | AppKit.NSResizableWindowMask
    )
    panel = StickyNotePanel.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(100, 100, DEFAULT_WIDTH, 400),
        style,
        AppKit.NSBackingStoreBuffered,
        False,
    )
    panel.setLevel_(AppKit.NSFloatingWindowLevel)
    panel.setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorStationary
    )
    panel.setMovableByWindowBackground_(True)
    panel.setBackgroundColor_(
        AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
            1.0, 0.992, 0.906, 1.0  # #FFFDE7
        )
    )
    panel.setOpaque_(False)
    panel.setHasShadow_(True)
    panel.setTitle_("Sticky Note")
    panel.setHidesOnDeactivate_(False)

    wk_config = WebKit.WKWebViewConfiguration.alloc().init()
    handler = StickyNoteMessageHandler.alloc().initWithPanel_journalPath_(
        panel, journal_path
    )
    wk_config.userContentController().addScriptMessageHandler_name_(handler, "stickynote")
    webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(
        panel.contentView().bounds(), wk_config
    )
    webview.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
    panel.contentView().addSubview_(webview)

    panel._webview = webview
    panel._handler = handler
    panel._gus = gus
    panel._journal_path = journal_path
    panel._collapsed = True
    panel._panel_collapsed = False

    panel.setDelegate_(panel)
    panel.load_content(gus, journal, now)
    panel.orderFrontRegardless()
    return panel


# ---------------------------------------------------------------------------
# Standalone __main__ — launched by the skill after Steps 1-3 are complete.
# Loads cached GUS data + journal path from the JSON written by the classify
# step, then runs a journal-only refresh loop (no MCP connection needed).
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    JSON_CACHE = "/tmp/sticky_note_data.json"

    # Config is the single source of truth for journal location and refresh
    # cadence. The launcher already validated user/journal consistency, but we
    # re-check here so a direct `python3 sticky_note.py` invocation fails loudly
    # rather than mixing two users' data.
    import sticky_note_config as _cfgmod
    try:
        _config = _cfgmod.load_config()
    except _cfgmod.ConfigError as _exc:
        print(f"[sticky-note] config error: {_exc}", file=sys.stderr)
        sys.exit(3)
    _JOURNAL_DIR = _config.journal_dir()

    def _journal_path_for(d: date) -> str:
        return str(_JOURNAL_DIR / (d.strftime("%Y-%m-%d") + "-journal.md"))

    try:
        with open(JSON_CACHE, "r", encoding="utf-8") as _f:
            _cached = json.load(_f)
        from sticky_note_gus import build_gus_data as _build_gus_data
        _gus = _build_gus_data(_cached["query_a"], _cached["query_b"], date.today())
        _journal_path: str = _cached["journal_path"]
    except Exception as _exc:
        print(f"[sticky-note] warning: could not load cached data ({_exc}); using empty state", file=sys.stderr)
        from sticky_note_gus import GusData as _GusData
        _gus = _GusData()
        _journal_path = _journal_path_for(date.today())

    _journal = load_journal(_journal_path)
    try:
        _cfgmod.check_user_conflict(_config.user, _journal.user_email)
    except _cfgmod.ConfigError as _exc:
        print(f"[sticky-note] config error: {_exc}", file=sys.stderr)
        sys.exit(3)
    _user_email: str = _journal.user_email
    _now = datetime.now()

    _app = AppKit.NSApplication.sharedApplication()
    # Regular policy: Dock icon present; lets NSFloatingWindowLevel work correctly
    # so the panel stays above normal windows when other apps are active.
    _app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    # Standard Edit menu so ⌘X/⌘C/⌘V/⌘A key equivalents reach the first
    # responder — without this, paste into the comment editor (NSTextView) and
    # copy from the agent-details WKWebView are dead (a borderless app has no
    # menu by default). Items use a nil target so AppKit routes them up the
    # responder chain to whatever field/view is focused.
    _install_edit_menu(_app)
    _app.finishLaunching()

    _panel = create_sticky_note_panel(_gus, _journal, _journal_path, _now)

    class _ReloadTarget(AppKit.NSObject):
        def reload_(self, _):
            try:
                _panel._gus = self._pending_gus
                _panel.load_content(self._pending_gus, self._pending_journal, datetime.now())
            except Exception as e:
                print(f"[sticky-note] reload error: {e}", file=sys.stderr)

    def _apply_gus_refresh(new_gus, done_streak_times):
        """Diff+save in background (already on bg thread), then dispatch UI reload to main thread."""
        today = date.today()
        today_path = _journal_path_for(today)
        from datetime import timedelta
        yesterday_path = _journal_path_for(today - timedelta(days=1))
        now = datetime.now()
        from sticky_note_gus import gus_items_for_diff
        # Hold _JOURNAL_LOCK across bootstrap/load -> diff -> save so a
        # concurrent UI mutation (checkbox/add/remove) can't be lost.
        with _JOURNAL_LOCK:
            j = bootstrap_journal(today_path, yesterday_path)
            _panel._journal_path = today_path
            filtered_gus = filter_stale_done(new_gus, done_streak_times, now)
            # Snapshot each WI's current category BEFORE the diff so we can tell
            # which WIs are newly entering the current-sprint TODO bucket.
            prev_categories = {
                name: (s.history[-1].category if s.history else None)
                for name, s in j.wi_state.items()
            }
            gus_items = gus_items_for_diff(filtered_gus, [])
            j, changed = diff_and_update(j, gus_items, now)
            # Auto-launch tcm-wi-worker for WIs that just joined current-sprint
            # TODO (no-op unless config.auto_invoke_wi_worker is true).
            if auto_invoke_wi_worker(j, filtered_gus, prev_categories, _config):
                changed = True
            if changed:
                save_journal(_panel._journal_path, j)
        target = _ReloadTarget.alloc().init()
        target._pending_gus = filtered_gus
        target._pending_journal = j
        target.performSelectorOnMainThread_withObject_waitUntilDone_(
            "reload:", None, False
        )

    # Refresh GUS data immediately in a background thread so startup is fast
    # but the panel shows fresh data within seconds.
    def _startup_refresh():
        import threading
        def _do_refresh():
            try:
                if not _user_email:
                    return
                result = refresh_gus_from_sf(_user_email, _panel._journal_path)
                if result is not None:
                    _apply_gus_refresh(*result)
            except Exception as _e:
                print(f"[sticky-note] startup refresh error: {_e}", file=sys.stderr)
        threading.Thread(target=_do_refresh, daemon=True).start()
    _startup_refresh()

    class _AppDelegate(AppKit.NSObject):
        def togglePanel_(self, sender):
            if _panel.isVisible():
                _panel.orderOut_(None)
            else:
                _panel.orderFrontRegardless()

        def otherAppActivated_(self, notification):
            _panel.orderFrontRegardless()

    _delegate = _AppDelegate.alloc().init()
    _app.setDelegate_(_delegate)

    # Menu-bar 📌 — toggle panel visibility from any app.
    _status_bar = AppKit.NSStatusBar.systemStatusBar()
    _status_item = _status_bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
    _status_item.button().setTitle_("📌")
    _status_item.button().setAction_("togglePanel:")
    _status_item.button().setTarget_(_delegate)

    # Re-raise panel whenever another app becomes the frontmost app.
    AppKit.NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
        _delegate,
        "otherAppActivated:",
        AppKit.NSWorkspaceDidActivateApplicationNotification,
        None,
    )

    class _JournalRefreshTarget(AppKit.NSObject):
        def fire_(self, _timer):
            import threading
            def _do():
                try:
                    result = None
                    if _user_email:
                        result = refresh_gus_from_sf(_user_email, _panel._journal_path)
                    if result is not None:
                        _apply_gus_refresh(*result)
                    else:
                        # GUS unavailable — reload journal on main thread
                        _j = load_journal(_panel._journal_path)
                        _t2 = _ReloadTarget.alloc().init()
                        _t2._pending_gus = _panel._gus
                        _t2._pending_journal = _j
                        _t2.performSelectorOnMainThread_withObject_waitUntilDone_(
                            "reload:", None, False
                        )
                except Exception as _e:
                    print(f"[sticky-note] refresh error: {_e}", file=sys.stderr)
            threading.Thread(target=_do, daemon=True).start()

    _target = _JournalRefreshTarget.alloc().init()
    _timer = Foundation.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
        _config.refresh_interval_seconds, _target, "fire:", None, True
    )
    Foundation.NSRunLoop.mainRunLoop().addTimer_forMode_(
        _timer, Foundation.NSDefaultRunLoopMode
    )

    # Lightning-bolt "refresh now": re-query GUS immediately AND slide the
    # repeating timer's next fire out a full interval, so a manual refresh
    # restarts the 15-minute clock rather than letting a tick land moments later.
    def _refresh_now():
        _target.fire_(None)
        _timer.setFireDate_(
            Foundation.NSDate.dateWithTimeIntervalSinceNow_(
                _config.refresh_interval_seconds
            )
        )
    _panel._on_refresh_now = _refresh_now

    # Background-agent poll: every agent_poll_interval_minutes, fold any finished
    # background agents' output into the journal, then re-render if it changed.
    # Runs on a bg thread and holds _JOURNAL_LOCK across the poll so it can't
    # clobber a concurrent GUS refresh or UI mutation.
    import sticky_note_agents as _agentsmod

    class _AgentPollTarget(AppKit.NSObject):
        def fire_(self, _timer):
            import threading

            def _do():
                try:
                    with _JOURNAL_LOCK:
                        changed = _agentsmod.poll_agents(_panel._journal_path)
                    if changed:
                        _j = load_journal(_panel._journal_path)
                        _t = _ReloadTarget.alloc().init()
                        _t._pending_gus = _panel._gus
                        _t._pending_journal = _j
                        _t.performSelectorOnMainThread_withObject_waitUntilDone_(
                            "reload:", None, False
                        )
                except Exception as _e:
                    print(f"[sticky-note] agent poll error: {_e}", file=sys.stderr)

            threading.Thread(target=_do, daemon=True).start()

    _agent_poll_target = _AgentPollTarget.alloc().init()
    _agent_poll_timer = Foundation.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
        _config.agent_poll_interval_seconds, _agent_poll_target, "fire:", None, True
    )
    Foundation.NSRunLoop.mainRunLoop().addTimer_forMode_(
        _agent_poll_timer, Foundation.NSDefaultRunLoopMode
    )

    # The repeating NSTimer does not fire while the machine is asleep, and macOS
    # does not replay the missed ticks on wake. Without this, a panel left through
    # a sleep/wake cycle keeps showing stale data (frozen last-refresh time) until
    # manually relaunched. Refresh immediately on wake — fire: ignores its argument,
    # so passing the notification is harmless and reuses the same refresh path.
    AppKit.NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
        _target,
        "fire:",
        AppKit.NSWorkspaceDidWakeNotification,
        None,
    )

    # External control socket: lets other processes signal "refresh now" so an
    # external journal edit (e.g. `sticky-note-add`) appears immediately instead
    # of waiting for the next timer tick. The handler runs on the socket's
    # daemon thread, so it only reuses the existing background-refresh helpers
    # (which already marshal the UI reload back to the main thread) — it never
    # touches AppKit directly.
    import sticky_note_control as _controlmod

    def _control_handler(cmd: dict) -> dict:
        action = cmd.get("cmd")
        if action != "refresh":
            return {"ok": False, "error": f"unknown cmd: {action!r}"}
        scope = cmd.get("scope", "journal")
        if scope == "gus":
            # Full re-query on a background thread (same path as the timer).
            _target.fire_(None)
            return {"ok": True, "scope": "gus"}
        if scope == "journal":
            # Cheap path: reload today's journal and re-render, reusing the
            # panel's cached GUS data. Done on a bg thread + marshaled to main.
            def _do():
                try:
                    with _JOURNAL_LOCK:
                        _j = load_journal(_panel._journal_path)
                    _t = _ReloadTarget.alloc().init()
                    _t._pending_gus = _panel._gus
                    _t._pending_journal = _j
                    _t.performSelectorOnMainThread_withObject_waitUntilDone_(
                        "reload:", None, False
                    )
                except Exception as _e:
                    print(f"[sticky-note] control refresh error: {_e}", file=sys.stderr)
            threading.Thread(target=_do, daemon=True).start()
            return {"ok": True, "scope": "journal"}
        return {"ok": False, "error": f"unknown scope: {scope!r}"}

    # Bind eagerly: a startup port collision is fatal (the socket is a declared
    # feature, not optional) — print and exit so the user can fix socket_port.
    try:
        _control_server = _controlmod.ControlServer(_config.socket_port, _control_handler)
    except OSError as _exc:
        print(
            f"[sticky-note] cannot bind control port {_config.socket_port}: {_exc}. "
            f"Change 'socket_port' in config.yaml if it collides with another service.",
            file=sys.stderr,
        )
        sys.exit(4)
    _control_server.start_thread()
    print(f"[sticky-note] control socket listening on 127.0.0.1:{_config.socket_port}")

    _app.activateIgnoringOtherApps_(True)
    _app.run()
