#!/usr/bin/env bash
# Recall SessionStart hook (v0.2.0).
# Reads $RECALL_HOME/pending/ for marker files written by the
# SessionEnd hook on prior sessions. If any markers exist, emits a
# system-reminder block so the agent (and the user) sees there's pending
# Recall work to do.
#
# Markers are NOT auto-deleted here — the /compound run clears them
# explicitly as part of its Stage 4 Tend. Manual clear:
#   rm $RECALL_HOME/pending/<session-id>.json
#
# Output: prints a system-reminder block to stdout (or nothing if no
# markers).

set -euo pipefail

# Configuration. Every path below resolves through an environment variable
# with a default; nothing is hardcoded to a particular machine or user.
#   RECALL_HOME         all mutable state       (default ~/.recall)
#   RECALL_CATALOG_DIR  the knowledge catalogs  (default $RECALL_HOME/catalogs)
#   RECALL_AGENT_BIN    CLI used by the drain   (default `claude`)
#   RECALL_SKILL_DIR    helper scripts          (default <this hook's dir>/../scripts)
RECALL_HOME="${RECALL_HOME:-$HOME/.recall}"
RECALL_CATALOG_DIR="${RECALL_CATALOG_DIR:-$RECALL_HOME/catalogs}"
RECALL_AGENT_BIN="${RECALL_AGENT_BIN:-claude}"
# Sibling scripts resolve relative to this file, never to an install location.
# `set -e` is active and a failed cd inside a command substitution would kill
# the whole hook, so fall back rather than abort.
SCRIPT_PARENT=$(dirname -- "${BASH_SOURCE[0]:-$0}")
SCRIPT_DIR=$(CDPATH= cd -P "$SCRIPT_PARENT" 2>/dev/null && pwd -P) || SCRIPT_DIR="."
RECALL_SKILL_DIR="${RECALL_SKILL_DIR:-$SCRIPT_DIR/../scripts}"

# --- running as a Claude Code plugin (2026-09-29) ----------------------------
# Claude Code exports CLAUDE_PLUGIN_ROOT only to a plugin's own hooks, so it is
# how this copy knows which install it belongs to. The plugin's skill is
# namespaced, so the command a person types differs too.
COMPOUND_CMD=/compound
if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
    COMPOUND_CMD=/recall:compound
    # A plugin has no install step, so its first session creates what install.sh
    # would have. Never inside the plugin folder: Claude Code replaces it on
    # every update. Never overwrites a catalog.
    for d in pending processed quarantine digests probe-state; do
        mkdir -p "$RECALL_HOME/$d" 2>/dev/null || true
    done
    mkdir -p "$RECALL_CATALOG_DIR" 2>/dev/null || true
    for c in FAILURE_MODES PROCESS_FAILURES DECISIONS; do
        if [ ! -e "$RECALL_CATALOG_DIR/$c.yaml" ]; then
            printf '# %s — filled as your sessions are distilled.\n[]\n' "$c" \
                > "$RECALL_CATALOG_DIR/$c.yaml" 2>/dev/null || true
        fi
    done
    # A settings.json hook from install.sh and this plugin's copy both run, so
    # every event would fire twice. Only the plugin copy checks, so the warning
    # prints once rather than once per copy.
    DUP_REPO=$(python3 - "${RECALL_HOST_DIR:-$HOME/.claude}/settings.json" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    for entries in (json.load(open(sys.argv[1])).get("hooks") or {}).values():
        for e in entries or []:
            for h in e.get("hooks") or []:
                cmd = str(h.get("command", ""))
                if "/hooks/recall-" in cmd:
                    print(cmd.split("/hooks/recall-")[0])
                    raise SystemExit(0)
except SystemExit:
    raise
except Exception:
    pass
PYEOF
)
    if [ -n "$DUP_REPO" ]; then
        echo "recall: every hook runs twice — the recall plugin and install.sh both registered them; run ${DUP_REPO}/install.sh --uninstall to keep only the plugin."
    fi
fi

# Surface a SessionEnd safe_load-sweep break (v0.3.1), once, then clear it.
BROKEN_MARKER="$RECALL_HOME/yaml-broken.json"
if [ -f "$BROKEN_MARKER" ]; then
    BLIST=$(jq -r '.[] | "  - \(.file): \(.error)"' "$BROKEN_MARKER" 2>/dev/null || cat "$BROKEN_MARKER")
    cat <<EOF
<system-reminder>
recall: knowledge YAML failed safe_load at last session end (structural break):
${BLIST}
Fix the file(s) by hand, then verify: python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" <file>
</system-reminder>
EOF
    rm -f "$BROKEN_MARKER"
fi

# Surface a stranded-catalog warning (v0.3.4), once, then clear it. The
# SessionEnd hook writes this marker when the catalog checkout has uncommitted
# catalog edits — a strand that can be wiped by a parallel-session branch
# switch before it's committed+pushed.
CATALOG_DIRTY_MARKER="$RECALL_HOME/catalog-dirty.json"
if [ -f "$CATALOG_DIRTY_MARKER" ]; then
    CDBRANCH=$(jq -r '.branch // "?"' "$CATALOG_DIRTY_MARKER" 2>/dev/null || echo "?")
    CDFILES=$(jq -r '.file_count // "?"' "$CATALOG_DIRTY_MARKER" 2>/dev/null || echo "?")
    # Same repo/pathspec split the SessionEnd hook uses to detect the strand.
    CATALOG_REPO_DIR=$(dirname -- "$RECALL_CATALOG_DIR")
    CATALOG_PATHSPEC=$(basename -- "$RECALL_CATALOG_DIR")
    cat <<EOF
<system-reminder>
recall: the catalogs had ${CDFILES} UNCOMMITTED file(s) on branch
'${CDBRANCH}' as of last session end. Uncommitted catalog edits strand
and can be wiped by a parallel-session branch switch
(PROCESS_FAILURES::uncommitted-fixes-wiped-by-parallel-session-branch-switch).
Reconcile: cd ${CATALOG_REPO_DIR} && git status -- ${CATALOG_PATHSPEC}/ — then
commit+push the new entries to main, or 'git checkout -- ${CATALOG_PATHSPEC}/'
if the content already lives on main.
</system-reminder>
EOF
    rm -f "$CATALOG_DIRTY_MARKER"
fi

# Surface an infrastructure outage (v0.3.7, 2026-09-12). The drain writes this
# when a run failed for a reason the session is not responsible for — expired
# auth, no credit, a missing login. Unlike the markers below it is NOT cleared
# here: a stale all-clear is what let this run silently for two days. The next
# successful drain run clears it.
AUTH_DOWN_MARKER="$RECALL_HOME/auth-down.json"
if [ -f "$AUTH_DOWN_MARKER" ]; then
    ADREASON=$(jq -r '.reason // "unknown"' "$AUTH_DOWN_MARKER" 2>/dev/null || echo "unknown")
    ADWHEN=$(jq -r '.detected_at // "unknown"' "$AUTH_DOWN_MARKER" 2>/dev/null || echo "unknown")
    # Same location the drain reads the token from, override included.
    ADCREDS="${RECALL_DRAIN_CREDENTIALS:-$RECALL_HOME/drain-credentials}"
    cat <<EOF
<system-reminder>
recall: the scheduled drain is NOT running. Last failure ${ADWHEN}:
  ${ADREASON}
Nothing is being distilled until this is fixed. The CLI authenticates
separately from the desktop app, so foreground sessions look healthy while the
drain is dead. Fix: \`${RECALL_AGENT_BIN} setup-token\` in a terminal, then store it —
  read -rs "?token: " T && printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\\n' "\$T" \\
    > ${ADCREDS} && chmod 600 ${ADCREDS}
Verify: ${SCRIPT_DIR}/recall-drain.sh && tail -5 ${RECALL_HOME}/drain.log
</system-reminder>
EOF
fi

# Surface quarantine depth (v0.3.7, 2026-09-12). Queue depth alone reports the
# OPPOSITE of the truth during a systemic outage: the retry path MOVES markers
# out of the pending dir, so a broken drain empties the queue and this hook
# then says nothing. Report the side location too, or do not report health.
QUARANTINE_DIR="$RECALL_HOME/quarantine"
if [ -d "$QUARANTINE_DIR" ]; then
    # Parked-for-size is a separate state from failed-three-times, and it is
    # reported separately. Both are counted: a subdirectory nothing counts is
    # exactly how 60 sessions went missing.
    # `set -e -o pipefail`: find on a missing dir fails, the pipeline inherits
    # it, and the assignment then kills this hook outright. Guard the directory
    # and neutralise the pipeline status -- counting nothing is a 0, not a fault.
    OCOUNT=0
    if [ -d "$QUARANTINE_DIR/_oversized" ]; then
        OCOUNT=$(find "$QUARANTINE_DIR/_oversized" -maxdepth 1 -type f -name '*.json' 2>/dev/null | wc -l | tr -d ' ' || true)
    fi
    if [ "${OCOUNT:-0}" -gt 0 ]; then
        cat <<EOF
<system-reminder>
recall: ${OCOUNT} session(s) parked as oversized — their transcripts
exceed what one distillation pass can hold, so the drain skips them rather than
timing out three times each. They are NOT lost and NOT counted above. To work
one by hand: ls ${QUARANTINE_DIR}/_oversized/
</system-reminder>
EOF
    fi
    QCOUNT=$(find "$QUARANTINE_DIR" -maxdepth 1 -type f -name '*.json' 2>/dev/null | wc -l | tr -d ' ' || true)
    if [ "$QCOUNT" -gt 0 ]; then
        cat <<EOF
<system-reminder>
recall: ${QCOUNT} session(s) sit in quarantine — captured but never
distilled. These are NOT counted in the pending queue. Inspect first (a large
count means a systemic failure, not ${QCOUNT} bad sessions), then requeue:
  mv ${QUARANTINE_DIR}/*.json ${RECALL_HOME}/pending/
</system-reminder>
EOF
    fi
fi

# Keep the probe index fresh (v0.3.7, 2026-09-12). The injector matches against
# a prebuilt index because the catalogs are 2.2 MB and cannot be parsed on every
# prompt. Rebuild only when a catalog actually moved, and in the background:
# this hook must not add seconds to session start, and a stale index degrades
# to "misses a new entry", never to a broken session.
PROBE_INDEX="$RECALL_HOME/probe-index.json"
PROBE_BUILDER="$RECALL_SKILL_DIR/build_probe_index.py"
if [ -x "$PROBE_BUILDER" ] || [ -r "$PROBE_BUILDER" ]; then
    # `set -e` is active: an && chain whose LAST test is false returns non-zero
    # and kills this whole hook, taking the pending/quarantine reports with it.
    # Observed exactly that. Use if-blocks, which set -e does not treat as an
    # error, rather than test chains.
    PROBE_STALE=0
    if [ ! -f "$PROBE_INDEX" ]; then
        PROBE_STALE=1
    else
        for c in "$RECALL_CATALOG_DIR/FAILURE_MODES.yaml" \
                 "$RECALL_CATALOG_DIR/PROCESS_FAILURES.yaml" \
                 "$RECALL_CATALOG_DIR/DECISIONS.yaml"; do
            if [ -f "$c" ] && [ "$c" -nt "$PROBE_INDEX" ]; then
                PROBE_STALE=1
            fi
        done
    fi
    if [ "$PROBE_STALE" -eq 1 ]; then
        (python3 "$PROBE_BUILDER" >/dev/null 2>&1 &)
    fi
fi

# --- digest reaper liveness (2026-09-13) ------------------------------------
# The drain reaps digests older than DIGEST_RETENTION_DAYS. That was very nearly
# shipped as dead code (the call sits one line above an empty-queue `exit 0`), so
# do not trust that it runs -- check the OUTCOME. A digest past retention plus a
# grace window, with no queued marker naming it, means nothing reaped it.
#
# Deliberately an invariant rather than a dated reminder: a date tells you about
# one day, this tells you whenever it breaks. Retention is read from the drain
# itself so the two cannot drift apart; 14 is only the parse fallback.
DIGEST_DIR="$RECALL_HOME/digests"
DRAIN_SRC="$SCRIPT_DIR/recall-drain.sh"
if [ -d "$DIGEST_DIR" ]; then
    DRET=""
    if [ -r "$DRAIN_SRC" ]; then
        DRET=$(sed -n 's/^DIGEST_RETENTION_DAYS="\${RECALL_DIGEST_RETENTION_DAYS:-\([0-9]\{1,\}\)}"/\1/p' "$DRAIN_SRC" 2>/dev/null | head -1 || true)
    fi
    case "${DRET:-}" in ''|*[!0-9]*) DRET=14 ;; esac
    DGRACE=$(( DRET + 3 ))
    STALE_N=0
    STALE_OLDEST=""
    while IFS= read -r df; do
        [ -n "$df" ] || continue
        dsid=$(basename "$df" .md)
        # a queued marker's transcript_path IS this digest -- not the reaper's fault
        if [ -f "$RECALL_HOME/pending/${dsid}.json" ]; then continue; fi
        STALE_N=$(( STALE_N + 1 ))
        [ -z "$STALE_OLDEST" ] && STALE_OLDEST=$(basename "$df")
    done <<EOF
$(find "$DIGEST_DIR" -maxdepth 1 -type f -name '*.md' -mtime +"$DGRACE" 2>/dev/null || true)
EOF
    if [ "${STALE_N:-0}" -gt 0 ]; then
        cat <<EOF
<system-reminder>
recall: ${STALE_N} digest(s) are older than ${DGRACE} days (retention
${DRET} + 3 grace) and nothing reaped them. reap_digests in the drain is not
running. It sits directly above \`[ "\$PENDING" -eq 0 ] && exit 0\` — if it was
moved below that guard it never runs on an idle queue, which is the steady state.
  Check:  grep reaped ${RECALL_HOME}/drain.log
  Test:   python3 ${SCRIPT_DIR}/../tests/test_drain_lifecycle.py
  Oldest: ${STALE_OLDEST}
</system-reminder>
EOF
    fi
fi
# --- end digest reaper liveness ---------------------------------------------

# --- compaction witness (2026-09-14) ----------------------------------------
# ABOVE the empty-queue exits on purpose. An empty queue is the common case and
# says nothing about whether the last session lost its context without writing
# anything down. Housekeeping placed below an early exit never runs in the
# steady state.
#
# Reports the number nothing else counts: how often a context window collapsed
# with no entry to show for it. recall-compaction-witness.py records the memory
# size at each compaction; this compares the earliest record for that session
# against the size now, and speaks only when it did not grow.
#
# Claude Code and Codex both fire PreCompact, so both hosts write this log.
COMPACTION_LOG="${RECALL_COMPACTION_LOG:-$RECALL_HOME/compaction-log.jsonl}"
if [ -r "$COMPACTION_LOG" ]; then
  CW=$(RECALL_CATALOG_DIR="$RECALL_CATALOG_DIR" python3 - "$COMPACTION_LOG" <<'PYEOF' 2>/dev/null || true
import json, os, sys, pathlib, collections
try:
    rows = []
    for line in open(sys.argv[1], encoding="utf-8"):
        line = line.strip()
        if line:
            try: rows.append(json.loads(line))
            except Exception: pass
    if not rows: raise SystemExit(0)
    by = collections.OrderedDict()
    for r in rows:
        by.setdefault(r.get("session_id", "unknown"), []).append(r)
    sid, recs = list(by.items())[-1]
    def total(e): return sum(v for v in (e or {}).values() if isinstance(v, int))
    then = total(recs[0].get("entries"))
    D = pathlib.Path(os.environ.get("RECALL_CATALOG_DIR") or
                     pathlib.Path.home() / ".recall/catalogs")
    now = 0
    for n in ("FAILURE_MODES", "PROCESS_FAILURES", "DECISIONS"):
        try:
            with open(D / f"{n}.yaml", "rb") as fh:
                now += sum(1 for ln in fh if ln.startswith(b"- "))
        except OSError: pass
    if not then or not now: raise SystemExit(0)
    if now - then <= 0:
        print(f"{len(recs)}|{sid[:8]}")
except SystemExit: raise
except Exception: pass
PYEOF
)
  if [ -n "$CW" ]; then
    CW_N=$(printf '%s' "$CW" | cut -d'|' -f1)
    CW_S=$(printf '%s' "$CW" | cut -d'|' -f2)
    cat <<EOF
<system-reminder>
recall: the last session to compact (${CW_S}) collapsed its context ${CW_N}
time(s) and your memory has not grown since. Compaction is the backstop, not
the capture -- if that session held anything worth keeping, it is now a summary
of itself.
</system-reminder>
EOF
  fi
fi
# --- end compaction witness -------------------------------------------------

# --- dated due-checks (2026-09-13) ------------------------------------------
# Handoffs accumulate follow-ups with dates ("re-audit in a few weeks", "compare
# X against Y on the 11th") and nothing ever fires them -- they depend on someone
# rereading a document. This surfaces them on and after their due date and stays
# silent before it. Entries are appended by any session; mark one done by adding
# "done" and "outcome" rather than deleting it, so the record survives.
DUE_CHECKS="$RECALL_HOME/due-checks.json"
if [ -r "$DUE_CHECKS" ]; then
    DUE_OUT=$(python3 - "$DUE_CHECKS" <<'PYEOF' 2>/dev/null || true
import json, sys, datetime
try:
    rows = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
if not isinstance(rows, list):
    sys.exit(0)
today = datetime.date.today()
out = []
for r in rows:
    if not isinstance(r, dict) or r.get("done"):
        continue
    try:
        due = datetime.date.fromisoformat(str(r.get("due", "")))
    except Exception:
        continue
    if due > today:
        continue
    over = (today - due).days
    when = "due today" if over == 0 else f"{over} day(s) overdue"
    out.append(f"  - {r.get('id','(unnamed)')} — {when} (due {due})")
    if r.get("what"):   out.append(f"      what: {r['what']}")
    if r.get("how"):    out.append(f"      how:  {r['how']}")
    if r.get("origin"): out.append(f"      from: {r['origin']}")
if out:
    print("\n".join(out))
PYEOF
)
    if [ -n "${DUE_OUT:-}" ]; then
        cat <<EOF
<system-reminder>
recall: dated check(s) now due —
${DUE_OUT}
Do the check, then record the result in ${DUE_CHECKS} by
adding "done": "<YYYY-MM-DD>" and "outcome": "<what you found>" to that entry.
Do NOT delete it: the outcome is the only evidence the check ever ran.
</system-reminder>
EOF
    fi
fi
# --- end dated due-checks ---------------------------------------------------

PENDING_DIR="$RECALL_HOME/pending"

[ ! -d "$PENDING_DIR" ] && exit 0

MARKERS=$(find "$PENDING_DIR" -maxdepth 1 -type f -name '*.json' 2>/dev/null)
[ -z "$MARKERS" ] && exit 0

COUNT=$(echo "$MARKERS" | wc -l | tr -d ' ')

# Build the body: one line per marker.
BODY=""
while IFS= read -r f; do
    [ -z "$f" ] && continue
    SID=$(jq -r '.session_id // "unknown"' "$f" 2>/dev/null || echo "unknown")
    TPATH=$(jq -r '.transcript_path // "unknown"' "$f" 2>/dev/null || echo "unknown")
    ENDED=$(jq -r '.ended_at // "unknown"' "$f" 2>/dev/null || echo "unknown")
    BODY="${BODY}  - ${SID} (ended ${ENDED}): transcript at ${TPATH}"$'\n'
done <<< "$MARKERS"

cat <<EOF
<system-reminder>
Recall has ${COUNT} pending session(s) to compound:
${BODY}Run ${COMPOUND_CMD} to process, or delete the marker(s) under
${PENDING_DIR}/ to dismiss.
</system-reminder>
EOF

exit 0
