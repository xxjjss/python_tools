#!/bin/bash
# Install / update the sticky-note widget to ~/.local/share/sticky-note/
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.local/share/sticky-note"

mkdir -p "$DEST" "$HOME/.local/bin"

cp "$SCRIPT_DIR"/sticky_note*.py \
   "$SCRIPT_DIR/sticky_note_template.html" \
   "$SCRIPT_DIR/config.yaml.template" \
   "$SCRIPT_DIR/launch_sticky_note.sh" \
   "$DEST/"

# Agent status icons (agent-running/done/lost/failed.png). Refresh the whole
# dir on every install.
rm -rf "$DEST/resource"
cp -R "$SCRIPT_DIR/resource" "$DEST/resource"

# Pre-encode each PNG as a base64 data URI written next to it (<png>.b64).
# WKWebView's loadHTMLString:baseURL: won't grant a file:// base read access to
# sibling files, so <img src="resource/*.png"> renders broken; the widget reads
# these .b64 files and inlines them as data: URIs instead. Encoding once here
# (not on every render) keeps the refresh loop cheap.
for png in "$DEST"/resource/*.png; do
    [ -e "$png" ] || continue
    printf 'data:image/png;base64,%s' "$(base64 < "$png" | tr -d '\n')" > "$png.b64"
done

chmod +x "$DEST/launch_sticky_note.sh"

# Config: never overwrite an existing per-user config.yaml (it holds the user's
# email + preferences). On a fresh install it's created from the template on
# first launch. On an upgrade of an EXISTING config, backfill only the top-level
# keys the template has but the config lacks (e.g. a newly-added setting like
# `agents`), copying the template's block verbatim — comments included — so the
# user picks up new defaults without losing anything they set. Existing keys are
# left exactly as-is.
CONFIG="$DEST/config.yaml"
TEMPLATE="$DEST/config.yaml.template"
if [ -f "$CONFIG" ] && [ -f "$TEMPLATE" ]; then
    python3 - "$CONFIG" "$TEMPLATE" <<'PY'
import re, sys
import yaml

config_path, template_path = sys.argv[1], sys.argv[2]

try:
    existing = yaml.safe_load(open(config_path, encoding="utf-8").read()) or {}
except Exception as exc:
    print(f"[install] config.yaml unparseable ({exc}); leaving it untouched")
    sys.exit(0)
if not isinstance(existing, dict):
    print("[install] config.yaml is not a mapping; leaving it untouched")
    sys.exit(0)

template_lines = open(template_path, encoding="utf-8").read().splitlines(keepends=True)

# Split the template into top-level blocks. A block is a top-level key plus its
# indented body, prefixed by any comment/blank lines that precede the key (so
# the explaining comment travels with the key when we append it).
KEY_RE = re.compile(r"^([^\s#][^:]*):")
blocks, prefix, current = [], [], None
for line in template_lines:
    m = KEY_RE.match(line)
    if m:                                  # new top-level key
        if current is not None:
            blocks.append(current)
        current = {"key": m.group(1), "lines": prefix + [line]}
        prefix = []
    elif current is not None and (line.startswith((" ", "\t")) and line.strip()):
        current["lines"].append(line)      # indented body of the current key
    else:                                   # comment / blank -> prefix next key
        prefix.append(line)
if current is not None:
    blocks.append(current)

missing = [b for b in blocks if b["key"] not in existing]
if not missing:
    sys.exit(0)

with open(config_path, encoding="utf-8") as fh:
    text = fh.read()
addition = ""
if text and not text.endswith("\n"):
    addition += "\n"
for b in missing:
    chunk = "".join(b["lines"])
    addition += "\n" + chunk if not chunk.startswith("\n") else chunk
with open(config_path, "a", encoding="utf-8") as fh:
    fh.write(addition)
print(f"[install] backfilled config.yaml with new keys: "
      f"{', '.join(b['key'] for b in missing)}")
PY
fi

# tcm-wi-researcher manifest: the agent reads ~/.claude/wi-researcher/manifest.yaml
# for its workspace_root + google_account. If the user doesn't already have one,
# seed it from the plugin's example manifest (../agents/ relative to this widget
# dir) and pre-fill google_account with the widget config's `user` email so a
# headless researcher run isn't blocked asking for it. Never overwrite an existing
# manifest — the user may have edited workspace_root / model.
MANIFEST_EXAMPLE="$SCRIPT_DIR/../agents/tcm-wi-researcher.manifest.example.yaml"
MANIFEST_DIR="$HOME/.claude/wi-researcher"
MANIFEST="$MANIFEST_DIR/manifest.yaml"
if [ ! -f "$MANIFEST" ] && [ -f "$MANIFEST_EXAMPLE" ]; then
    mkdir -p "$MANIFEST_DIR"
    python3 - "$MANIFEST_EXAMPLE" "$MANIFEST" "$CONFIG" <<'PY'
import re, sys
import yaml

example_path, manifest_path, config_path = sys.argv[1], sys.argv[2], sys.argv[3]

# The config's `user` email, if a config exists and carries a non-empty user.
user = ""
try:
    cfg = yaml.safe_load(open(config_path, encoding="utf-8").read()) or {}
    if isinstance(cfg, dict):
        user = (cfg.get("user") or "").strip()
except Exception:
    pass  # no config yet (fresh install) — leave the example placeholder

text = open(example_path, encoding="utf-8").read()
if user:
    # Replace only the google_account VALUE, preserving its comment block. Match a
    # top-level `google_account:` line (not indented, not a comment).
    text, n = re.subn(
        r"(?m)^google_account:[ \t]*.*$", f"google_account: {user}", text
    )
    if not n:
        print("[install] manifest: no google_account line to fill; wrote example as-is")

with open(manifest_path, "w", encoding="utf-8") as fh:
    fh.write(text)
print(f"[install] seeded wi-researcher manifest at {manifest_path}"
      + (f" (google_account={user})" if user else " (edit google_account before use)"))
PY
fi

# Create entry point if not present
ENTRY="$HOME/.local/bin/sticky-note"
if [ ! -f "$ENTRY" ]; then
    cat > "$ENTRY" <<'EOF'
#!/bin/bash
exec bash "$(dirname "$(readlink -f "$0")")/../share/sticky-note/launch_sticky_note.sh" "$@"
EOF
    chmod +x "$ENTRY"
fi

# Entry point for adding a Non-WI task from outside the widget:
#   sticky-note-add "<title>" ["<comment>"]
ADD_ENTRY="$HOME/.local/bin/sticky-note-add"
cat > "$ADD_ENTRY" <<'EOF'
#!/bin/bash
DIR="$(dirname "$(readlink -f "$0")")/../share/sticky-note"
exec python3 "$DIR/sticky_note_add_task.py" "$@"
EOF
chmod +x "$ADD_ENTRY"

# Entry point for signalling the running widget to refresh immediately:
#   sticky-note-refresh [--scope journal|gus]
REFRESH_ENTRY="$HOME/.local/bin/sticky-note-refresh"
cat > "$REFRESH_ENTRY" <<'EOF'
#!/bin/bash
DIR="$(dirname "$(readlink -f "$0")")/../share/sticky-note"
exec python3 "$DIR/sticky_note_refresh_cli.py" "$@"
EOF
chmod +x "$REFRESH_ENTRY"

# Entry point for the Non-WI task CRUD API (put/get/update/delete):
#   sticky-note-task put '{"title":"...","comment":"..."}'
TASK_ENTRY="$HOME/.local/bin/sticky-note-task"
cat > "$TASK_ENTRY" <<'EOF'
#!/bin/bash
DIR="$(dirname "$(readlink -f "$0")")/../share/sticky-note"
exec python3 "$DIR/sticky_note_task.py" "$@"
EOF
chmod +x "$TASK_ENTRY"

echo "Installed to $DEST"
echo "Run: sticky-note"
echo "Add a task: sticky-note-add \"<title>\" [\"<comment>\"]"
echo "Task API: sticky-note-task <put|get|update|delete> [args]"
echo "Refresh now: sticky-note-refresh [--scope journal|gus]"
