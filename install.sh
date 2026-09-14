#!/usr/bin/env bash
# install.sh — wire the compound loop into your agent.
#
# This edits your agent's settings file and installs a scheduled job. Both are
# things you should be able to undo without reading the source, so: it prints a
# plan, asks once, writes a timestamped backup of anything it modifies, and
# leaves an uninstall script that reverses exactly what it did.
#
#   ./install.sh              plan, confirm, install
#   ./install.sh --dry-run    plan only, change nothing
#   ./install.sh --uninstall  reverse a previous install
#
# Registration is per machine and deliberately not automatic: a repo that wires
# itself into your agent on clone is not a thing you can audit before it runs.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOUND_HOME="${COMPOUND_HOME:-$HOME/.compound}"
COMPOUND_CATALOG_DIR="${COMPOUND_CATALOG_DIR:-$COMPOUND_HOME/catalogs}"
COMPOUND_HOST_DIR="${COMPOUND_HOST_DIR:-$HOME/.claude}"
SETTINGS="$COMPOUND_HOST_DIR/settings.json"
STAMP="$(date +%Y%m%d-%H%M%S)"
DRY=0
UNINSTALL=0

for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing dependency: $1" >&2; exit 1; }; }

# ---------------------------------------------------------------- preflight --
need python3
need jq
python3 -c 'import yaml' 2>/dev/null || {
  echo "missing Python package: PyYAML  (pip install pyyaml)" >&2; exit 1; }

if [ ! -f "$SETTINGS" ]; then
  echo "no agent settings at $SETTINGS" >&2
  echo "set COMPOUND_HOST_DIR to your agent's directory and re-run." >&2
  exit 1
fi
python3 -c "import json,sys;json.load(open('$SETTINGS'))" 2>/dev/null || {
  echo "$SETTINGS is not valid JSON — refusing to touch it" >&2; exit 1; }

# ---------------------------------------------------------------- uninstall --
if [ "$UNINSTALL" -eq 1 ]; then
  say "Removing compound hooks from $SETTINGS (state under $COMPOUND_HOME is kept)."
  cp "$SETTINGS" "$SETTINGS.compound-backup-$STAMP"
  python3 - "$SETTINGS" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
hooks = d.get("hooks", {})
for ev in list(hooks):
    for entry in hooks[ev]:
        entry["hooks"] = [h for h in entry.get("hooks", [])
                          if "compound-" not in h.get("command", "")]
    hooks[ev] = [e for e in hooks[ev] if e.get("hooks")]
    if not hooks[ev]:
        del hooks[ev]
json.dump(d, open(p, "w"), indent=2)
print("  hooks removed")
PY
  if command -v launchctl >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)/compound.drain" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/compound.drain.plist"
    say "  scheduled drain removed"
  else
    say "  remove the compound drain line from your crontab by hand"
  fi
  say "Done. Backup: $SETTINGS.compound-backup-$STAMP"
  exit 0
fi

# --------------------------------------------------------------------- plan --
say ""
say "compound — install plan"
say ""
say "  repo         $REPO_DIR"
say "  state        $COMPOUND_HOME            (created if absent)"
say "  catalogs     $COMPOUND_CATALOG_DIR"
say "  agent dir    $COMPOUND_HOST_DIR"
say ""
say "  1. create $COMPOUND_HOME/{pending,processed,quarantine,digests,probe-state}"
say "  2. seed empty catalogs if none exist (never overwrites)"
say "  3. register 4 hooks in $SETTINGS:"
say "       SessionEnd        capture a finished session"
say "       SessionStart      report queue + retrieval health"
say "       UserPromptSubmit  match your prompt against the corpus"
say "       PostToolUse       match file edits against the corpus"
say "  4. schedule the drain every 15 minutes"
say ""
say "  A backup of settings.json is written before any edit."
say "  Reverse everything with: ./install.sh --uninstall"
say ""

if [ "$DRY" -eq 1 ]; then say "(dry run — nothing changed)"; exit 0; fi
printf 'proceed? [y/N] '
read -r reply
case "$reply" in y|Y|yes|YES) ;; *) say "aborted"; exit 0 ;; esac

# ------------------------------------------------------------------ install --
mkdir -p "$COMPOUND_HOME"/{pending,processed,quarantine,digests,probe-state}
mkdir -p "$COMPOUND_CATALOG_DIR"
for c in FAILURE_MODES PROCESS_FAILURES; do
  f="$COMPOUND_CATALOG_DIR/$c.yaml"
  # Never clobber a corpus. An install that can destroy months of captured work
  # is worse than one that fails.
  [ -e "$f" ] || printf '# %s — filled by the drain as your sessions are distilled.\n[]\n' "$c" > "$f"
done
say "  state ready at $COMPOUND_HOME"

cp "$SETTINGS" "$SETTINGS.compound-backup-$STAMP"
python3 - "$SETTINGS" "$REPO_DIR" <<'PY'
import json, sys
settings, repo = sys.argv[1], sys.argv[2]
d = json.load(open(settings))
hooks = d.setdefault("hooks", {})
WIRING = [
    ("SessionEnd",       f"{repo}/hooks/compound-sessionend.sh"),
    ("SessionStart",     f"{repo}/hooks/compound-sessionstart.sh"),
    ("UserPromptSubmit", f"{repo}/hooks/compound-probe-inject.sh"),
    ("PostToolUse",      f"{repo}/hooks/compound-probe-inject.sh"),
]
added = 0
for event, cmd in WIRING:
    entries = hooks.setdefault(event, [])
    if any(cmd == h.get("command")
           for e in entries for h in e.get("hooks", [])):
        continue                      # idempotent: re-running installs nothing twice
    entries.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
    added += 1
json.dump(d, open(settings, "w"), indent=2)
print(f"  {added} hook(s) registered ({len(WIRING) - added} already present)")
PY

if command -v launchctl >/dev/null 2>&1; then
  PLIST="$HOME/Library/LaunchAgents/compound.drain.plist"
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>compound.drain</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$REPO_DIR/hooks/compound-drain.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>COMPOUND_HOME</key><string>$COMPOUND_HOME</string>
        <key>COMPOUND_CATALOG_DIR</key><string>$COMPOUND_CATALOG_DIR</string>
        <key>COMPOUND_HOST_DIR</key><string>$COMPOUND_HOST_DIR</string></dict>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><false/>
</dict></plist>
PLISTEOF
  launchctl bootout "gui/$(id -u)/compound.drain" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  say "  drain scheduled every 900s"
else
  say "  launchctl not found — add this to your crontab:"
  say "    */15 * * * * COMPOUND_HOME=$COMPOUND_HOME bash $REPO_DIR/hooks/compound-drain.sh"
fi

say ""
say "Installed. The loop starts capturing when your next session ends."
say ""
say "  try it:   python3 $REPO_DIR/scripts/recall.py --stats"
say "  undo it:  $REPO_DIR/install.sh --uninstall"
say ""
