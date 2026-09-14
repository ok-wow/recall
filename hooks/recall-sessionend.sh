#!/usr/bin/env bash
# Recall SessionEnd hook (v0.2.0).
# Writes a pending-marker IF (a) the transcript holds real work AND (b) no /compound
# was invoked this session. The next session's SessionStart hook surfaces
# any pending markers as a system-reminder so the user can run
# /compound on the prior session's transcript.
#
# Input: stdin JSON from the host agent's hook system, containing at least
#   { session_id, transcript_path }
# Output: writes $RECALL_HOME/pending/<session-id>.json
#         (silent if conditions not met)

set -euo pipefail

RECALL_HOME="${RECALL_HOME:-$HOME/.recall}"
RECALL_CATALOG_DIR="${RECALL_CATALOG_DIR:-$RECALL_HOME/catalogs}"

# Resolve sibling scripts beside this file, so a second copy of the hook
# installed under another agent's hook directory uses that copy's scripts
# instead of reaching across into the first install.
# `set -euo pipefail` is active. An unset BASH_SOURCE[0] is fatal under `set -u`,
# and a failing cd makes the substitution non-zero, which `set -e` turns into an
# immediate exit -- before the marker is ever written, so the session is dropped
# from the queue silently. Guard both, exactly as recall-sessionstart.sh does.
SCRIPT_DIR=$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd -P) || SCRIPT_DIR="."
RECALL_SKILL_DIR="${RECALL_SKILL_DIR:-$SCRIPT_DIR/../scripts}"

PENDING_DIR="$RECALL_HOME/pending"

mkdir -p "$PENDING_DIR"

INPUT=$(cat)

# --- safe_load sweep (v0.3.1) -----------------------------------------------
# Belt-and-suspenders catch-all: the PostToolUse YAML hook only fires when the
# written path appears in the tool call. A Python writer that builds the path
# dynamically slips past it. This sweep safe_loads every knowledge file at
# session end regardless of how it was written; a break writes a marker the
# SessionStart hook surfaces next session.
# The three catalogs and nothing else. Four other paths used to be listed here
# -- learnings.yaml, extracted_registry.yaml, artifacts_registry.yaml,
# orphans/learnings.yaml -- carried over from the system this was extracted
# from. Nothing in this repo creates, reads or documents any of them, so the
# sweep was checking four files that never exist.
SWEEP_FILES=(
  "$RECALL_CATALOG_DIR/FAILURE_MODES.yaml"
  "$RECALL_CATALOG_DIR/PROCESS_FAILURES.yaml"
  "$RECALL_CATALOG_DIR/DECISIONS.yaml"
)
SWEEP_EXISTING=()
for f in "${SWEEP_FILES[@]}"; do [ -f "$f" ] && SWEEP_EXISTING+=("$f"); done
BROKEN_MARKER="$RECALL_HOME/yaml-broken.json"
if [ ${#SWEEP_EXISTING[@]} -gt 0 ]; then
  if SWEEP_BAD=$(python3 - "${SWEEP_EXISTING[@]}" <<'PY'
import sys, yaml, json
bad = []
for f in sys.argv[1:]:
    try:
        with open(f) as fh:
            yaml.safe_load(fh)
    except Exception as e:
        first = str(e).splitlines()[0] if str(e) else ""
        bad.append({"file": f, "error": f"{type(e).__name__}: {first}"})
if bad:
    print(json.dumps(bad))
    sys.exit(1)
PY
  ); then
    rm -f "$BROKEN_MARKER"   # all three parse — clear any stale marker
  else
    printf '%s' "$SWEEP_BAD" > "$BROKEN_MARKER"
    printf 'recall: SessionEnd safe_load sweep found broken knowledge YAML: %s\n' "$SWEEP_BAD" >&2
  fi
fi
# --- end safe_load sweep ----------------------------------------------------

# --- catalog strand detection (v0.3.4, 2026-06-19) --------------------------
# A /compound run appends to the catalogs (often symlinked into a git working
# tree) and is supposed to commit+push (the Stage-4 Tend step). If that step is
# skipped/interrupted, or appends land while the catalog checkout sits on a
# feature branch, catalog edits strand as uncommitted WIP — silently exposed to
# a parallel-session branch-switch wipe
# (PROCESS_FAILURES::uncommitted-fixes-wiped-by-parallel-session-branch-switch).
# Detect uncommitted catalog changes at session end and write a marker the
# SessionStart hook surfaces next session. Detection, not auto-mutation:
# auto-switching the checkout could clobber an active feature session.
CATALOG_REPO_DIR=$(dirname -- "$RECALL_CATALOG_DIR")
CATALOG_PATHSPEC=$(basename -- "$RECALL_CATALOG_DIR")
CATALOG_DIRTY_MARKER="$RECALL_HOME/catalog-dirty.json"
if [ -e "$CATALOG_REPO_DIR/.git" ]; then
  DIRTY=$(git -C "$CATALOG_REPO_DIR" status --porcelain -- "$CATALOG_PATHSPEC/" 2>/dev/null || true)
  if [ -n "$DIRTY" ]; then
    CBRANCH=$(git -C "$CATALOG_REPO_DIR" branch --show-current 2>/dev/null || echo "?")
    CNFILES=$(printf '%s\n' "$DIRTY" | grep -c . || true)
    cat > "$CATALOG_DIRTY_MARKER" <<EOF
{
  "branch": "$CBRANCH",
  "file_count": $CNFILES,
  "status": $(printf '%s' "$DIRTY" | jq -Rs .),
  "detected_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
  else
    rm -f "$CATALOG_DIRTY_MARKER"   # clean tree — clear any stale marker
  fi
fi
# --- end catalog strand detection ------------------------------------------

SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty')

# Bail quietly if we don't have what we need.
[ -z "$SESSION_ID" ] && exit 0
[ -z "$TRANSCRIPT_PATH" ] && exit 0
[ ! -f "$TRANSCRIPT_PATH" ] && exit 0

# Size is still recorded (the drain's oversize gate reads it) but no longer
# decides anything.
SIZE=$(stat -f%z "$TRANSCRIPT_PATH" 2>/dev/null || stat -c%s "$TRANSCRIPT_PATH" 2>/dev/null || echo 0)

# Condition (a): the session contains actual work.
#
# This was an 80 KB byte threshold and bytes measured the wrong thing. The
# harness injects skill/agent/MCP listings as `attachment` records before the
# first turn, so an auth-keepalive probe whose whole conversation is
# "reply with the single word: ok" weighs 87 KB and sailed over the bar. Seven
# of them reached the drain and distilled into nothing, while a genuinely
# useful 60 KB session would never have been queued at all. Over the 55-session
# queue of 2026-09-12 the populations do not overlap: probes are 1 user turn and
# 0 tool calls, real work starts at 631 turns and 599 tool calls. So count turns
# and tool calls, in both the Claude Code and Codex schemas.
#
# Exit 1 from the classifier means "not substantive". Under `set -e` that would
# kill this hook outright, so it is consumed by an `if`, never left bare.
SUBSTANCE="$RECALL_SKILL_DIR/transcript_substance.py"
if [ -r "$SUBSTANCE" ]; then
    if command -v timeout >/dev/null 2>&1; then
        SUBSTANCE_RUN=(timeout 5 python3 "$SUBSTANCE" "$TRANSCRIPT_PATH")
    else
        SUBSTANCE_RUN=(python3 "$SUBSTANCE" "$TRANSCRIPT_PATH")
    fi
    if SUBSTANCE_OUT=$("${SUBSTANCE_RUN[@]}" 2>/dev/null); then
        :   # substantive, or the classifier failed open — queue it
    else
        SUBSTANCE_RC=$?
        # rc 1 is a confident "no". Anything else (timeout 124, crash) is not a
        # verdict, and dropping a session on a broken classifier loses signal
        # permanently — so only rc 1 skips.
        if [ "$SUBSTANCE_RC" -eq 1 ]; then
            exit 0
        fi
    fi
fi

# Condition (b): no /compound was invoked this session.
# Match the command with or without an argument. The scheduled drain passes a
# session ID after the command, so an exact quoted-string match would queue the
# drain's own session and make the queue grow while it drains.
# Three shapes, because the hosts record an invoked command differently:
#   Claude Code  <command-name>/compound</command-name>
#   Claude Code  "content": "/compound ..."   (plain string content)
#   Codex        "text":"/compound ..."       (block inside payload.content[])
if grep -Eq '"(content|text)"[[:space:]]*:[[:space:]]*"/compound([[:space:]]|")|<command-name>/compound</command-name>' "$TRANSCRIPT_PATH" 2>/dev/null; then
    exit 0
fi

# Condition (c): this session has not already been processed.
#
# The drain writes a per-session record when a worker completes, whatever the
# verdict. Without it the only "was this done?" signal was a grep for the
# literal string /compound in the transcript, which cannot see a drain
# that ran out of band -- so drained sessions were re-enqueued and re-distilled
# only to be discarded as already-present. PROCESS_FAILURES prescribed exactly
# this record on 2026-06-04 and it was never built; it went unbuilt long enough
# for the same gap to resurface from the opposite direction.
PROCESSED_RECORD="$RECALL_HOME/processed/${SESSION_ID}.json"
if [ -f "$PROCESSED_RECORD" ]; then
    exit 0
fi

# All conditions met — write the marker.
MARKER_FILE="$PENDING_DIR/${SESSION_ID}.json"
cat > "$MARKER_FILE" <<EOF
{
  "session_id": "$SESSION_ID",
  "transcript_path": "$TRANSCRIPT_PATH",
  "ended_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "transcript_size_bytes": $SIZE,
  "reason": "substantive-transcript-no-compound-invocation"
}
EOF

exit 0
