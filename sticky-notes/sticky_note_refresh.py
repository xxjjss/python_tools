"""Refresh loop and AppKit entry point for the sticky-note widget."""

from __future__ import annotations

import os
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Callable

import AppKit
import Foundation
import objc

from sticky_note_journal import load_journal, bootstrap_journal, diff_and_update, save_journal
from sticky_note_gus import build_gus_data, gus_items_for_diff
from sticky_note import StickyNotePanel, create_sticky_note_panel

REFRESH_INTERVAL = 900.0  # 15 minutes


def _today_journal_path(current_path: str) -> str:
    """Return today's journal path in the same directory as current_path."""
    parent = Path(current_path).parent
    return str(parent / (date.today().strftime("%Y-%m-%d") + "-journal.md"))


def do_refresh(
    panel: StickyNotePanel,
    journal_path: str,
    query_a_fn: Callable[[], list[dict]],
    query_b_fn: Callable[[], list[dict]],
) -> None:
    """Re-run Steps 2–5: fetch GUS, diff journal, save if changed, re-render."""
    try:
        a_records = query_a_fn()
        b_records = query_b_fn()
        today = date.today()
        now = datetime.now()
        gus = build_gus_data(a_records, b_records, today)
        journal = load_journal(journal_path)
        gus_items = gus_items_for_diff(gus, [])
        journal, changed = diff_and_update(journal, gus_items, now)
        if changed:
            save_journal(journal_path, journal)
        panel._gus = gus
        panel.load_content(gus, journal, now)
    except Exception as exc:
        print(f"[sticky-note] refresh error: {exc}", file=sys.stderr)


class _RefreshTarget(AppKit.NSObject):
    def initWithPanel_journalPath_queryA_queryB_(
        self, panel, journal_path, query_a_fn, query_b_fn
    ):
        self = objc.super(_RefreshTarget, self).init()
        if self is None:
            return None
        self._panel = panel
        self._journal_path = journal_path
        self._query_a_fn = query_a_fn
        self._query_b_fn = query_b_fn
        return self

    def fire_(self, timer):
        today_path = _today_journal_path(self._journal_path)
        if today_path != self._journal_path:
            # Midnight rollover: bootstrap new day's file, carry forward Others
            try:
                bootstrap_journal(today_path, self._journal_path)
            except Exception as exc:
                print(f"[sticky-note] day-rollover error: {exc}", file=sys.stderr)
            self._journal_path = today_path
            self._panel._journal_path = today_path
        do_refresh(self._panel, self._journal_path, self._query_a_fn, self._query_b_fn)


def schedule_refresh(
    panel: StickyNotePanel,
    journal_path: str,
    query_a_fn: Callable[[], list[dict]],
    query_b_fn: Callable[[], list[dict]],
) -> Foundation.NSTimer:
    target = _RefreshTarget.alloc().initWithPanel_journalPath_queryA_queryB_(
        panel, journal_path, query_a_fn, query_b_fn
    )
    timer = Foundation.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
        REFRESH_INTERVAL, target, "fire:", None, True
    )
    Foundation.NSRunLoop.mainRunLoop().addTimer_forMode_(
        timer, Foundation.NSDefaultRunLoopMode
    )
    return timer


def main(
    journal_path: str,
    query_a_fn: Callable[[], list[dict]],
    query_b_fn: Callable[[], list[dict]],
) -> None:
    expanded_path = os.path.expanduser(journal_path)
    today = date.today()
    yesterday_path = str(Path(expanded_path).parent / (
        (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        + "-journal.md"
    ))

    journal = bootstrap_journal(expanded_path, yesterday_path)

    try:
        a_records = query_a_fn()
        b_records = query_b_fn()
    except Exception as exc:
        print(f"[sticky-note] initial GUS fetch failed: {exc}", file=sys.stderr)
        a_records, b_records = [], []

    now = datetime.now()
    gus = build_gus_data(a_records, b_records, today)
    gus_items = gus_items_for_diff(gus, [])
    journal, changed = diff_and_update(journal, gus_items, now)
    if changed:
        save_journal(expanded_path, journal)

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    panel = create_sticky_note_panel(gus, journal, expanded_path, now)
    schedule_refresh(panel, expanded_path, query_a_fn, query_b_fn)

    app.run()
