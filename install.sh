#!/usr/bin/env bash
# install.sh — wire the okWOW • Recall loop into your agent.
#
# This edits your agent's settings file and installs a scheduled job. Both are
# things you should be able to undo without reading the source, so: it prints a
# plan, asks once, writes a timestamped backup of anything it modifies, and
# leaves an uninstall script that reverses exactly what it did.
#
#   ./install.sh              plan, confirm, install
#   ./install.sh --host codex install for Codex instead of Claude Code
#   ./install.sh --dry-run    plan only, change nothing
#   ./install.sh --uninstall  reverse a previous install
#
# Registration is per machine and deliberately not automatic: a repo that wires
# itself into your agent on clone is not a thing you can audit before it runs.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECALL_HOME="${RECALL_HOME:-$HOME/.recall}"
RECALL_CATALOG_DIR="${RECALL_CATALOG_DIR:-$RECALL_HOME/catalogs}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DRY=0
UNINSTALL=0

for a in "$@"; do
  case "$a" in
    --host=*) HOST="${a#*=}" ;;
    --host) WANT_HOST=1 ;;
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) if [ "${WANT_HOST:-0}" = 1 ]; then HOST="$a"; WANT_HOST=0;
       else echo "unknown option: $a" >&2; exit 2; fi ;;
  esac
done

# Claude Code and Codex take the SAME hook structure --
#   {"hooks": {"SessionEnd": [{"hooks": [{"type","command","timeout"}]}]}}
# -- in different files. That is the entire difference between the two hosts,
# so the registration code below is shared and only these three lines vary.
HOST="${HOST:-}"
if [ -z "$HOST" ]; then
  if   [ -f "$HOME/.claude/settings.json" ]; then HOST=claude
  elif [ -f "$HOME/.codex/hooks.json" ];     then HOST=codex
  else HOST=claude; fi
fi
case "$HOST" in
  claude) RECALL_HOST_DIR="${RECALL_HOST_DIR:-$HOME/.claude}"
          SETTINGS="$RECALL_HOST_DIR/settings.json"; DEFAULT_BIN=claude ;;
  codex)  RECALL_HOST_DIR="${RECALL_HOST_DIR:-$HOME/.codex}"
          SETTINGS="$RECALL_HOST_DIR/hooks.json";    DEFAULT_BIN=codex ;;
  *) echo "unknown host: $HOST (expected claude or codex)" >&2; exit 2 ;;
esac
SKILL_DEST="${RECALL_SKILL_DEST:-$RECALL_HOST_DIR/skills/compound}"

say() { printf '%s\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing dependency: $1" >&2; exit 1; }; }

# ---------------------------------------------------------------- preflight --
need python3
need jq
python3 -c 'import yaml' 2>/dev/null || {
  echo "missing Python package: PyYAML  (pip install pyyaml)" >&2; exit 1; }

if [ ! -f "$SETTINGS" ]; then
  echo "no agent settings at $SETTINGS" >&2
  echo "set RECALL_HOST_DIR to your agent's directory and re-run." >&2
  exit 1
fi
python3 -c "import json,sys;json.load(open('$SETTINGS'))" 2>/dev/null || {
  echo "$SETTINGS is not valid JSON — refusing to touch it" >&2; exit 1; }

# ---------------------------------------------------------------- uninstall --
if [ "$UNINSTALL" -eq 1 ]; then
  say "Removing Recall hooks from $SETTINGS (state under $RECALL_HOME is kept)."
  cp "$SETTINGS" "$SETTINGS.recall-backup-$STAMP"
  python3 - "$SETTINGS" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
hooks = d.get("hooks", {})
for ev in list(hooks):
    for entry in hooks[ev]:
        entry["hooks"] = [h for h in entry.get("hooks", [])
                          if "recall-" not in h.get("command", "")]
    hooks[ev] = [e for e in hooks[ev] if e.get("hooks")]
    if not hooks[ev]:
        del hooks[ev]
json.dump(d, open(p, "w"), indent=2)
print("  hooks removed")
PY
  # Only ever remove our own symlink, never a directory someone put there.
  if [ -L "$SKILL_DEST" ]; then
    rm -f "$SKILL_DEST"; say "  /compound skill unlinked"
  fi
  if command -v launchctl >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)/ai.okwow.recall-drain" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/ai.okwow.recall-drain.plist"
    say "  scheduled drain removed"
  else
    say "  remove the Recall drain line from your crontab by hand"
  fi
  say "Done. Backup: $SETTINGS.recall-backup-$STAMP"
  exit 0
fi

# --------------------------------------------------------------------- plan --
say ""
say "okWOW • Recall — install plan  (host: $HOST)"
say ""
say "  repo         $REPO_DIR"
say "  state        $RECALL_HOME            (created if absent)"
say "  catalogs     $RECALL_CATALOG_DIR"
say "  agent dir    $RECALL_HOST_DIR"
say "  hook config  $SETTINGS"
say ""
say "  1. create $RECALL_HOME/{pending,processed,quarantine,digests,probe-state}"
say "  2. seed empty catalogs if none exist (never overwrites)"
say "  3. register the hooks in $SETTINGS:"
say "       SessionEnd        capture a finished session"
say "       SessionStart      report queue + retrieval health"
say "       UserPromptSubmit  match your prompt against the corpus"
say "       PostToolUse       match file edits against the corpus"
[ "$HOST" = claude ] && say "       PreCompact        note when a context window collapses"
say "  4. install the /compound skill into $SKILL_DEST"
say "  5. schedule the drain every 15 minutes"
say ""
say "  A backup of settings.json is written before any edit."
say "  Reverse everything with: ./install.sh --uninstall"
say ""

if [ "$DRY" -eq 1 ]; then say "(dry run — nothing changed)"; exit 0; fi
printf 'proceed? [y/N] '
read -r reply
case "$reply" in y|Y|yes|YES) ;; *) say "aborted"; exit 0 ;; esac

# ------------------------------------------------------------------ install --
mkdir -p "$RECALL_HOME"/{pending,processed,quarantine,digests,probe-state}
mkdir -p "$RECALL_CATALOG_DIR"
for c in FAILURE_MODES PROCESS_FAILURES DECISIONS; do
  f="$RECALL_CATALOG_DIR/$c.yaml"
  # Never clobber a corpus. An install that can destroy months of captured work
  # is worse than one that fails.
  [ -e "$f" ] || printf '# %s — filled by the drain as your sessions are distilled.\n[]\n' "$c" > "$f"
done
say "  state ready at $RECALL_HOME"

# The drain invokes "/compound"; without this the loop captures and drains and
# then writes nothing, leaving an empty corpus that looks like a quiet one.
# Symlinked rather than copied so `git pull` updates the skill with the code.
mkdir -p "$(dirname "$SKILL_DEST")"
if [ -e "$SKILL_DEST" ] && [ ! -L "$SKILL_DEST" ]; then
  say "  a real directory already sits at $SKILL_DEST — leaving it alone"
else
  ln -sfn "$REPO_DIR/skills/compound" "$SKILL_DEST"
  say "  /compound skill linked -> $SKILL_DEST"
fi

cp "$SETTINGS" "$SETTINGS.recall-backup-$STAMP"
python3 - "$SETTINGS" "$REPO_DIR" "$HOST" <<'PY'
import json, sys
settings, repo, host = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(settings))
hooks = d.setdefault("hooks", {})
WIRING = [
    ("SessionEnd",       f"{repo}/hooks/recall-sessionend.sh"),
    ("SessionStart",     f"{repo}/hooks/recall-sessionstart.sh"),
    ("UserPromptSubmit", f"{repo}/hooks/recall-probe-inject.sh"),
    ("PostToolUse",      f"{repo}/hooks/recall-probe-inject.sh"),
]
# Claude Code only. Codex's event list has no PreCompact, and registering an
# event a host never fires would look wired and do nothing -- the shape of bug
# this project exists to catch. On Codex the witness is simply absent and its
# SessionStart reader stays inert, which is honest.
if host == "claude":
    WIRING.append(("PreCompact", f"{repo}/hooks/recall-compaction-witness.py"))
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
  PLIST="$HOME/Library/LaunchAgents/ai.okwow.recall-drain.plist"
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.okwow.recall-drain</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$REPO_DIR/hooks/recall-drain.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>RECALL_HOME</key><string>$RECALL_HOME</string>
        <key>RECALL_CATALOG_DIR</key><string>$RECALL_CATALOG_DIR</string>
        <key>RECALL_HOST_DIR</key><string>$RECALL_HOST_DIR</string>
        <key>RECALL_AGENT_BIN</key><string>${RECALL_AGENT_BIN:-$DEFAULT_BIN}</string></dict>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><false/>
</dict></plist>
PLISTEOF
  launchctl bootout "gui/$(id -u)/ai.okwow.recall-drain" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  say "  drain scheduled every 900s"
else
  say "  launchctl not found — add this to your crontab:"
  say "    */15 * * * * RECALL_HOME=$RECALL_HOME bash $REPO_DIR/hooks/recall-drain.sh"
fi

say ""
say "Installed. The loop starts capturing when your next session ends."
say ""
say "  try it:   python3 $REPO_DIR/scripts/recall.py --stats"
say "  parked:   python3 $REPO_DIR/scripts/park.py list"
say "  undo it:  $REPO_DIR/install.sh --uninstall"
say ""
