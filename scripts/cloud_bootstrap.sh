#!/usr/bin/env bash
# Set Recall up inside an ephemeral cloud container. Safe to run twice.
#
# Paste into the cloud environment's setup / SessionStart command:
#
#   git clone --depth 1 https://github.com/ok-wow/recall ~/recall && bash ~/recall/scripts/cloud_bootstrap.sh
#
# Mode (RECALL_BOOTSTRAP_MODE): "plugin" runs
#   claude plugin marketplace add ok-wow/recall && claude plugin install recall@ok-wow
# and is the default when `claude` is on PATH; "hooks" registers this checkout's hooks
# (read from hooks/hooks.json) in ~/.claude/settings.json. If the plugin commands fail it
# falls back to hooks, and it always exits 0.
#
# It sets RECALL_HOME to a container path and turns cloud sync on, through the settings
# `env` block. For the digest to reach your machine the container also needs
#   RECALL_PERSONAL_REMOTE   your recall-personal repo URL
#   RECALL_SYNC_TOKEN        a token that can push to it (a container secret; never written here)
# Without them, sync degrades to a local outbox and says so in every receipt.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export RECALL_HOME="${RECALL_HOME:-/tmp/recall-cloud}"
HOST_DIR="${RECALL_HOST_DIR:-$HOME/.claude}"
SETTINGS="$HOST_DIR/settings.json"
MODE="${RECALL_BOOTSTRAP_MODE:-}"
if [ -z "$MODE" ]; then if command -v claude >/dev/null 2>&1; then MODE=plugin; else MODE=hooks; fi; fi

mkdir -p "$HOST_DIR" "$RECALL_HOME"/{pending,processed,quarantine,digests,probe-state,inbox,outbox,catalogs} 2>/dev/null || true
[ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"

if [ "$MODE" = plugin ]; then
  if claude plugin marketplace add ok-wow/recall >/dev/null 2>&1 && claude plugin install recall@ok-wow >/dev/null 2>&1; then
    echo "recall: plugin installed (recall@ok-wow)"
  else
    echo "recall: plugin install failed, falling back to hooks from $REPO_DIR" >&2
    MODE=hooks
  fi
fi

python3 - "$SETTINGS" "$REPO_DIR" "$MODE" "$RECALL_HOME" <<'PY'
import json, sys
settings, repo, mode, home = sys.argv[1:5]
d = json.load(open(settings))
before = json.dumps(d, sort_keys=True)
env = d.setdefault("env", {})
env.setdefault("RECALL_HOME", home)
env["RECALL_SYNC"] = "1"
if mode == "hooks":
    plugin = json.load(open(f"{repo}/hooks/hooks.json"))["hooks"]
    hooks = d.setdefault("hooks", {})
    for event, groups in plugin.items():
        entries = hooks.setdefault(event, [])
        for g in groups:
            for h in g["hooks"]:
                cmd = h["command"].replace("${CLAUDE_PLUGIN_ROOT}", repo)
                if any(cmd == x.get("command") for e in entries for x in e.get("hooks", [])):
                    continue
                entries.append({"hooks": [{**h, "command": cmd}]})
json.dump(d, open(settings, "w"), indent=2)
print("recall: nothing changed, already set up" if json.dumps(d, sort_keys=True) == before
      else f"recall: set up ({mode}); RECALL_HOME={env['RECALL_HOME']}, cloud sync on")
PY

[ -n "${CLAUDE_ENV_FILE:-}" ] && printf 'export RECALL_HOME=%q RECALL_SYNC=1\n' "$RECALL_HOME" >> "$CLAUDE_ENV_FILE"
[ -n "${RECALL_PERSONAL_REMOTE:-}" ] || echo "recall: RECALL_PERSONAL_REMOTE is not set; digests stay in $RECALL_HOME/outbox and receipts will say NOT synced"
exit 0
