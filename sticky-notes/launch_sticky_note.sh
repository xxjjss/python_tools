#!/bin/bash
# Launch the sticky-note widget as a background process.
# On first run: prompts for GUS login (browser SSO) and user email.
# On subsequent runs: reuses stored credentials and email from journal.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SF_BIN="$HOME/.aisuite/bin/sf"
GUS_ALIAS="gus"

# ---------------------------------------------------------------------------
# Ensure sf is logged in to GUS org
# ---------------------------------------------------------------------------
if ! "$SF_BIN" org display --target-org "$GUS_ALIAS" --json > /dev/null 2>&1; then
    echo "GUS org not logged in. Opening browser for authentication..."
    "$SF_BIN" org login web --alias "$GUS_ALIAS" --instance-url https://gus.my.salesforce.com
    if ! "$SF_BIN" org display --target-org "$GUS_ALIAS" --json > /dev/null 2>&1; then
        echo "Login failed or cancelled. Widget will start with cached/empty GUS data."
    else
        echo "Login successful."
    fi
fi

# Kill any existing instance
EXISTING=$(pgrep -f "sticky_note.py" 2>/dev/null)
if [ -n "$EXISTING" ]; then
    echo "Stopping existing widget (PID $EXISTING)..."
    kill "$EXISTING" 2>/dev/null
    sleep 1
fi

# ---------------------------------------------------------------------------
# Resolve config (creates config.yaml from the template on first run), derive
# the journal directory from config.journal_path, and reconcile the user email
# between config.user and the email recorded in the journal.
# ---------------------------------------------------------------------------
export PYTHONPATH="$SCRIPT_DIR"
TODAY=$(python3 -c "from datetime import date; print(date.today().strftime('%Y-%m-%d'))")
YESTERDAY=$(python3 -c "from datetime import date, timedelta; print((date.today()-timedelta(days=1)).strftime('%Y-%m-%d'))")

# Prints two lines to stdout on success: journal_dir, then the resolved email
# (may be empty). Exits non-zero and prints the reason to stderr on a
# config/user conflict, so the launcher stops before touching any data.
CONFIG_OUT=$(python3 - <<PYEOF
import sys
sys.path.insert(0, '$SCRIPT_DIR')
from datetime import date, timedelta
import sticky_note_config as cfgmod
from sticky_note_journal import load_journal

try:
    cfg = cfgmod.load_config()
    jd = cfg.journal_dir()
    tp = jd / (date.today().strftime('%Y-%m-%d') + '-journal.md')
    yp = jd / ((date.today() - timedelta(days=1)).strftime('%Y-%m-%d') + '-journal.md')

    def read_email(p):
        try:
            return load_journal(str(p)).user_email
        except Exception:
            return ''

    journal_email = read_email(tp) or read_email(yp)
    cfgmod.check_user_conflict(cfg.user, journal_email)
    print(str(jd))
    print(cfg.user or journal_email)
except cfgmod.ConfigError as exc:
    sys.stderr.write('[sticky-note] config error: %s\n' % exc)
    sys.exit(3)
PYEOF
)
if [ $? -ne 0 ]; then
    echo "Exiting due to config error."
    exit 1
fi

JOURNAL_DIR=$(printf '%s\n' "$CONFIG_OUT" | sed -n '1p')
STORED_EMAIL=$(printf '%s\n' "$CONFIG_OUT" | sed -n '2p')
JOURNAL_PATH="$JOURNAL_DIR/${TODAY}-journal.md"
YESTERDAY_PATH="$JOURNAL_DIR/${YESTERDAY}-journal.md"

if [ -z "$STORED_EMAIL" ]; then
    echo ""
    read -r -p "Enter your GUS email address: " USER_EMAIL
    if [ -z "$USER_EMAIL" ]; then
        echo "Email is required. Exiting."
        exit 1
    fi
    STORED_EMAIL="$USER_EMAIL"
else
    echo "Using stored email: $STORED_EMAIL"
fi

# ---------------------------------------------------------------------------
# Persist the resolved email to BOTH config.user and the journal (idempotent),
# then bootstrap today's journal (creates file if absent, carries tasks over).
# ---------------------------------------------------------------------------
python3 - "$STORED_EMAIL" "$JOURNAL_PATH" "$YESTERDAY_PATH" <<PYEOF
import sys
sys.path.insert(0, '$SCRIPT_DIR')
import sticky_note_config as cfgmod
from sticky_note_journal import bootstrap_journal, save_journal

email, today_path, yesterday_path = sys.argv[1], sys.argv[2], sys.argv[3]
cfgmod.set_user(email)
journal = bootstrap_journal(today_path, yesterday_path)
if journal.user_email != email:
    journal.user_email = email
    save_journal(today_path, journal)
print('[sticky-note] user synced to config + journal; journal bootstrapped for $TODAY')
PYEOF

python3 "$SCRIPT_DIR/sticky_note.py" \
    > /tmp/sticky_note_stdout.txt \
    2> /tmp/sticky_note_stderr.txt &
SN_PID=$!
disown "$SN_PID"
sleep 3

if ps -p "$SN_PID" > /dev/null 2>&1; then
    echo "Sticky Note widget running (PID $SN_PID)"
else
    echo "Widget failed to start. Check /tmp/sticky_note_stderr.txt:"
    cat /tmp/sticky_note_stderr.txt
    exit 1
fi
