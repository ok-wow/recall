#!/usr/bin/env bash
# Recall bounded drain (v3, 2026-09-13).
# v3 owns processed-marker records and digests oversized transcripts before work.
#
# Runs the four-stage distillation on a schedule so throughput stops depending
# on someone remembering to type /compound. Registration is deliberately
# NOT automatic: see hooks.md. Install it as a periodic job per machine.
#
# One session per invocation, looped — not one invocation draining many.
# The thing that degrades with volume is context, not count: distilling the
# fifteenth session inside a context already holding fourteen produces worse
# work and can trip host compaction mid-run. A fresh process per session is the
# only way to actually guarantee a clean context, and it drops the blast radius
# of a bad run from N sessions to one. A count cap was the old proxy for this;
# it throttled throughput without preventing the failure it named.
#
# Script-enforced: a new process per iteration, a hard wall-clock budget, and a
# no-progress break. Skill-cooperative: whether an invocation distills exactly
# the session it was handed. The context-freshness property holds either way.
#
# Why scheduled rather than SessionEnd: a SessionEnd hook gets one second by
# default and at most three (hooks.md), and a child spawned from it dies with
# its parent's process group when the host reaps the timeout. Recall appends
# to shared YAML, so a run killed mid-write is exactly the corruption this
# system must not have.
#
# Env: RECALL_DRAIN_BUDGET (total seconds, default 1800)
#      RECALL_DRAIN_SESSION_TIMEOUT (per session, default 600)
#      RECALL_AGENT_BIN (agent CLI, default `claude`)

set -uo pipefail

RECALL_HOME="${RECALL_HOME:-$HOME/.recall}"
RECALL_CATALOG_DIR="${RECALL_CATALOG_DIR:-$RECALL_HOME/catalogs}"
# The worker resolves the same state paths; export so an operator override here
# is the one the child sees too, instead of each side falling back separately.
export RECALL_HOME RECALL_CATALOG_DIR

# Resolve this script's own directory so a second copy of the hook installed
# under another agent's hook directory uses that copy's siblings.
case "$0" in
  */*) SCRIPT_PARENT=${0%/*} ;;
  *) SCRIPT_PARENT=. ;;
esac
SCRIPT_DIR=$(CDPATH= cd -P "$SCRIPT_PARENT" 2>/dev/null && pwd -P) || SCRIPT_DIR="."
SKILL_DIR="${RECALL_SKILL_DIR:-$SCRIPT_DIR/../scripts}"

PENDING_DIR="$RECALL_HOME/pending"
LOCK_DIR="$RECALL_HOME/drain.lock"
QUARANTINE_DIR="$RECALL_HOME/quarantine"
ATTEMPTS_DIR="$RECALL_HOME/drain-attempts"
LOG="$RECALL_HOME/drain.log"
# The default is a bare command name, so it is resolved on PATH below before the
# executable check — `[ -x claude ]` would test a file in the current directory.
AGENT_BIN="${RECALL_AGENT_BIN:-claude}"
# Deny rules for the unattended run only. Loaded per invocation so the operator's
# interactive sessions keep their own posture: a global rule would silently
# restrict work the operator is present for.
PERMISSIONS="${RECALL_DRAIN_PERMISSIONS:-$SCRIPT_DIR/recall-drain-permissions.json}"
BUDGET="${RECALL_DRAIN_BUDGET:-1800}"
SESSION_TIMEOUT="${RECALL_DRAIN_SESSION_TIMEOUT:-600}"
# The worker does not inherit the operator's interactive model. The host's
# settings pin an expensive long-context model for foreground work; the
# unattended run picked that up and died on "out of usage credits" the moment
# auth started working. Distillation is a read-and-summarize job, so pin the
# cheaper model here and leave the operator's choice alone.
MODEL="${RECALL_DRAIN_MODEL:-haiku}"
DRAIN_VERSION="3"

# Unattended auth. The interactive app and this CLI do not share a credential.
# The app kept working for two days while every drain run failed to
# authenticate, and the retry path moved 60 sessions into quarantine rather
# than saying so. `setup-token` issues a long-lived token that does not
# depend on a refreshable session; keep it here at 0600 and load it per run.
AUTH_DOWN_MARKER="$RECALL_HOME/auth-down.json"
# The per-session "this was processed" record. Its absence is the root cause
# behind two separate failures three months apart: in June, SessionEnd
# re-enqueued sessions that had already been drained; today, finished sessions
# stranded in the queue. Both are the same gap -- nothing durable said "done".
PROCESSED_DIR="$RECALL_HOME/processed"

# Record the verdict, then the marker can go. Deleting a marker with no record
# is exactly what SKILL.md warns against ("silent auto-clear loses signal"), so
# the record is written FIRST and the delete only follows a successful write.
record_processed() {
  _sess="$1"; _verdict="$2"; _runlog="$3"
  mkdir -p "$PROCESSED_DIR" 2>/dev/null || return 1
  _tail=$(tail -c 2000 "$_runlog" 2>/dev/null || printf '')
  cat > "$PROCESSED_DIR/$_sess.json" <<LEDGER
{
  "session_id": "$_sess",
  "processed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "verdict": "$_verdict",
  "model": "$MODEL",
  "run_log_tail": $(printf '%s' "$_tail" | jq -Rs .)
}
LEDGER
  [ -s "$PROCESSED_DIR/$_sess.json" ]
}
# Lines that mean "the infrastructure is down", not "this session is bad".
# Anchored to line start: the CLI prints these as bare lines, while the
# worker's own prose quotes the same phrases mid-sentence when it reports on a
# session it just distilled successfully. Written as ONE variable rather than
# inline, because inlining it produced '^('a|b')' — a shell pipeline that
# passes `bash -n` and greps for nothing.
INFRA_RE='^(Failed to authenticate|OAuth session expired|Invalid API key|Not logged in|Credit balance|You.re out of usage credits|Usage limit reached)'
# Exit 0 does NOT mean the work happened. `claude -p "/compound <id>"` prints
# "Unknown command: /compound" and exits 0 when the skill is not registered for
# that invocation -- a dangling symlink, a clone that moved, or the crontab path
# that forgets RECALL_AGENT_BIN. The drain used to read that 0 as success, write
# the ledger, delete the marker, and SessionEnd then refused to re-queue the
# session: destroyed, permanently, one per tick, while the queue looked healthy.
# The refuting evidence was already in the run log it saved and nobody read it.
WORKER_BROKEN_RE='^(Unknown command|Unknown slash command|Invalid command)'

# A transcript no context window can hold is not a retry candidate. Without
# this gate the giants (95 KB .. 149 MB in the Sep-12 queue) each burn three
# 600s timeouts before quarantining, and `ls -tr` puts them in front of work
# that would have succeeded. Park them where the SessionStart report can still
# see them -- a silent hole is the bug this whole exercise started from.
OVERSIZE_BYTES="${RECALL_DRAIN_OVERSIZE_BYTES:-8388608}"   # 8 MB
OVERSIZED_DIR="$QUARANTINE_DIR/_oversized"
DIGEST_DIR="$RECALL_HOME/digests"
DIGESTER="$SKILL_DIR/digest_transcript.py"
DIGEST_RETENTION_DAYS="${RECALL_DIGEST_RETENTION_DAYS:-14}"

# cron hands a job almost no environment, and the agent CLI reads its
# credentials from the login Keychain, which it cannot find without USER: the
# run reports "Not logged in - Please run /login" and the drain makes no
# progress, hourly, in silence. Set what the scheduler may not, rather than
# depending on it.
export USER="${USER:-$(id -un)}"
export HOME="${HOME:-$(eval echo ~"$(id -un)")}"
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG"; }

# BELOW log() ON PURPOSE. This block aborts via log(), and on macOS an
# undefined log() resolves to /usr/bin/log, which prints its own usage to
# stderr and returns 0 -- so the abort vanished and the drain died silently.
# The fail-closed guard further down carries the same warning; this is the
# second time the same trap was walked into in the same file.
CREDS="${RECALL_DRAIN_CREDENTIALS:-$RECALL_HOME/drain-credentials}"
# This file used to be `.`-sourced, which runs arbitrary shell as you every 15
# minutes from launchd -- and a line in it could reassign $PERMISSIONS and point
# the worker at a decoy deny list, defeating the guard below. It now yields only
# KEY=VALUE, and only from a file that is yours and mode 0600, because a token
# readable by other local users is not a secret.
if [ -e "$CREDS" ]; then
  _cmode=$(stat -f '%Lp' "$CREDS" 2>/dev/null || stat -c '%a' "$CREDS" 2>/dev/null || echo "")
  _cown=$(stat -f '%u' "$CREDS" 2>/dev/null || stat -c '%u' "$CREDS" 2>/dev/null || echo "")
  if [ "$_cown" != "$(id -u)" ]; then
    log "abort: $CREDS is not owned by $(id -un) — refusing to read it"
    exit 0
  elif [ "$_cmode" != "600" ]; then
    log "abort: $CREDS is mode ${_cmode:-unknown}, expected 600 — run: chmod 600 '$CREDS'"
    exit 0
  else
    # Only NAME=VALUE lines, one variable per line, nothing executed.
    while IFS= read -r _line || [ -n "$_line" ]; do
      case "$_line" in
        ''|\#*) continue ;;
        [A-Za-z_]*=*)
          _k=${_line%%=*}
          case "$_k" in *[!A-Za-z0-9_]*) continue ;; esac
          _v=${_line#*=}
          _v=${_v%\"}; _v=${_v#\"}; _v=${_v%\'}; _v=${_v#\'}
          export "$_k=$_v"
          ;;
      esac
    done < "$CREDS"
  fi
fi

# FAIL CLOSED. The worker runs --permission-mode auto so Stage 4 can commit
# without a human to approve each write; that is only defensible while the deny
# rules exist. A missing file must therefore HALT, not degrade into "auto mode
# with no deny list" and not into an obscure per-invocation error from the agent
# CLI. This was a real defect: an extraction renamed the reference and left the
# file behind, shipping the permissive mode and the reassuring comment without
# the guard they describe.
if [ ! -r "$PERMISSIONS" ]; then
  log "abort: deny rules not readable at $PERMISSIONS — refusing to run an unattended agent in auto mode without them"
  exit 0
fi
if ! python3 -c "import json,sys;d=json.load(open(sys.argv[1]));sys.exit(0 if d.get('permissions',{}).get('deny') else 1)" "$PERMISSIONS" 2>/dev/null; then
  log "abort: $PERMISSIONS has no permissions.deny rules — refusing to run unattended in auto mode"
  exit 0
fi

# Reduce an oversized transcript to a bounded digest and repoint the marker at
# it. The worker is handed a session id and reads the marker's transcript_path,
# so pointing that at the digest needs no new instruction in the skill -- the
# same reason marker lifecycle moved here: a structural fact beats asking the
# worker in prose. Returns non-zero when nothing usable came out, and the
# caller then parks exactly as before.
digest_marker() {
  _m="$1"; _s="$2"
  [ -r "$DIGESTER" ] || return 1
  _t=$(jq -r '.transcript_path // empty' "$PENDING_DIR/$_m" 2>/dev/null) || return 1
  [ -n "$_t" ] && [ -f "$_t" ] || return 1
  mkdir -p "$DIGEST_DIR" || return 1
  _d="$DIGEST_DIR/$_s.md"
  # IDEMPOTENCE. After the first pass this marker's transcript_path points AT
  # the digest, so a retry would hand the reducer input == output and truncate
  # the digest to zero before reading it. digest_of holds the ORIGINAL path, so
  # a lost digest can still be rebuilt from source rather than from itself.
  _prior=$(jq -r '.digest_of // empty' "$PENDING_DIR/$_m" 2>/dev/null || true)
  if [ -n "$_prior" ]; then
    [ -s "$_d" ] && return 0
    [ -f "$_prior" ] || return 1
    _t="$_prior"
  fi
  # Exit 2 means the digest was too thin to be worth a worker run.
  python3 "$DIGESTER" "$_t" "$_d" >/dev/null 2>&1 || return 1
  # Rewrite the marker atomically: a half-written marker that still names the
  # giant transcript would send the worker straight back into the timeout.
  # Record the DIGEST's size, not the original's. The oversize gate reads this
  # field rather than stat-ing the file, so leaving it at the original's size
  # keeps the marker permanently classified as oversized and re-enters this
  # branch on every later run for a session that is no longer large.
  _dsize=$(stat -f%z "$_d" 2>/dev/null || stat -c%s "$_d" 2>/dev/null || echo 0)
  case "$_dsize" in ''|*[!0-9]*) _dsize=0 ;; esac
  jq --arg d "$_d" --arg o "$_t" --argjson n "$_dsize" \
     '.transcript_path = $d | .digest_of = $o | .transcript_size_bytes = $n | .reason = "digested-oversize"' \
     "$PENDING_DIR/$_m" > "$PENDING_DIR/.$_m.tmp" 2>/dev/null || {
       rm -f "$PENDING_DIR/.$_m.tmp"; return 1; }
  mv "$PENDING_DIR/.$_m.tmp" "$PENDING_DIR/$_m" || return 1
  return 0
}
count_pending() { ls "$PENDING_DIR" 2>/dev/null | wc -l | tr -d ' '; }

# A digest is a derived artifact with exactly one consumer: the worker draining
# the marker that points at it. Nothing ever removed them, so the directory grew
# by one file per oversized session forever. Reap by age, but never a digest a
# queued marker still names -- once digest_marker() repoints it, that digest IS
# the marker's transcript_path, and deleting it sends the worker to a dead path.
reap_digests() {
  [ -d "$DIGEST_DIR" ] || return 0
  _reaped=0
  while IFS= read -r _f; do
    [ -n "$_f" ] || continue
    _sid=$(basename "$_f" .md)
    # A marker anywhere still referencing this digest keeps it alive, not just a
    # PENDING one. digest_marker() repoints transcript_path at the digest BEFORE
    # the worker runs, so a marker that later fails into quarantine still names
    # it -- reaping then makes the documented requeue hand the worker a dead path.
    # Only search directories that exist. `find a b` with b missing exits
    # non-zero, and under `set -o pipefail` that makes the whole pipeline
    # non-zero even though grep matched -- so the guard silently inverts and the
    # reaper deletes the digest it was protecting. Caught by a fixture, not by
    # reading: FAILURE_MODES::set-e-pipefail-turns-a-benign-count-into-a-silent-whole-hook-exit
    _search=""
    [ -d "$PENDING_DIR" ] && _search="$PENDING_DIR"
    [ -d "$QUARANTINE_DIR" ] && _search="$_search $QUARANTINE_DIR"
    if [ -n "$_search" ]; then
      # shellcheck disable=SC2086
      _refs=$(find $_search -type f -name "$_sid.json" 2>/dev/null || true)
      [ -n "$_refs" ] && continue
    fi
    rm -f "$_f" && _reaped=$((_reaped + 1))
  done < <(find "$DIGEST_DIR" -name '*.md' -type f -mtime +"$DIGEST_RETENTION_DAYS" 2>/dev/null)
  [ "$_reaped" -gt 0 ] && log "reaped $_reaped digest(s) older than ${DIGEST_RETENTION_DAYS}d"
  return 0
}
# Oldest marker not already attempted in this run. Retrying a marker inside the
# same run does not help: the failures worth retrying are transient (load, rate
# limit), and the live pair that motivated this were ten minutes apart in
# separate runs. Back-to-back retries just burn the timeout three times over.
oldest_marker() {
  ls -tr "$PENDING_DIR" 2>/dev/null | while read -r f; do
    grep -qxF "$f" "$LOCK_DIR/attempted" 2>/dev/null || { printf '%s\n' "$f"; return; }
  done
}

# The drain is itself an agent session. Without this guard its own session end
# or a nested invocation would re-enter the drain.
[ "${RECALL_WORKER:-}" = "1" ] && exit 0

# Deliberately ABOVE the empty-queue exit below. Steady state is an empty queue,
# so housekeeping placed after that guard would never run on the common path.
reap_digests

PENDING=$(count_pending)
[ "$PENDING" -eq 0 ] && exit 0
AGENT_PATH=$(command -v "$AGENT_BIN" 2>/dev/null || printf '')
[ -n "$AGENT_PATH" ] && [ -x "$AGENT_PATH" ] || { log "abort: no agent binary for '$AGENT_BIN'"; exit 0; }

# Atomic claim. A held lock means a drain is mid-flight; this tick is a no-op.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  LOCK_PID=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
  if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
    log "skip: drain already running (pid $LOCK_PID)"
    exit 0
  fi
  log "reaping stale lock (pid ${LOCK_PID:-unknown} gone)"
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR" 2>/dev/null || { log "skip: lost lock race"; exit 0; }
fi
echo "$$" > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

# Stage 4 commits the catalogs, so the worker starts where they live. The dir
# is state under RECALL_HOME like pending/ and processed/, which this script
# already creates on demand — an operator override or a state dir that has not
# been through install.sh must not cost the whole run. The abort still stands
# for a path that cannot be made (unwritable, or a file in the way).
mkdir -p "$RECALL_CATALOG_DIR" 2>/dev/null
cd "$RECALL_CATALOG_DIR" || { log "abort: cannot cd $RECALL_CATALOG_DIR"; exit 0; }
log "start: v${DRAIN_VERSION}, $PENDING pending, budget ${BUDGET}s"

STARTED=$(date +%s)
DRAINED=0
QUARANTINED=0
MAX_QUARANTINE="${RECALL_DRAIN_MAX_QUARANTINE:-3}"
MAX_ATTEMPTS="${RECALL_DRAIN_MAX_ATTEMPTS:-3}"
while :; do
  REMAINING=$(count_pending)
  [ "$REMAINING" -eq 0 ] && { log "queue empty"; break; }

  ELAPSED=$(( $(date +%s) - STARTED ))
  if [ "$ELAPSED" -ge "$BUDGET" ]; then
    log "budget spent after ${ELAPSED}s"
    break
  fi

  MARKER=$(oldest_marker)
  [ -z "$MARKER" ] && { log "no marker left to try this run"; break; }
  SESSION="${MARKER%.json}"
  printf '%s\n' "$MARKER" >> "$LOCK_DIR/attempted"

  MSIZE=$(jq -r '.transcript_size_bytes // 0' "$PENDING_DIR/$MARKER" 2>/dev/null || echo 0)
  case "$MSIZE" in ''|*[!0-9]*) MSIZE=0 ;; esac
  if [ "$MSIZE" -gt "$OVERSIZE_BYTES" ]; then
    # Parking used to be the whole answer, and it leaked: 15 sessions, 1.1 GB,
    # accumulated in _oversized where nothing ever processed them -- and the
    # biggest transcripts are the longest working sessions, so the loop was
    # dropping its richest signal. Digest first; park only if that fails.
    if digest_marker "$MARKER" "$SESSION"; then
      DSIZE=$(stat -f%z "$DIGEST_DIR/$SESSION.md" 2>/dev/null || echo 0)
      log "digested $SESSION: $((MSIZE / 1048576)) MB -> $((DSIZE / 1024)) KB, draining now"
    else
      mkdir -p "$OVERSIZED_DIR"
      if mv "$PENDING_DIR/$MARKER" "$OVERSIZED_DIR/$MARKER" 2>/dev/null; then
        log "oversized: $SESSION is $((MSIZE / 1048576)) MB and would not digest — parked, no attempt consumed"
      fi
      continue
    fi
  fi

  # 'auto' plus a deny list, rather than bypassPermissions. Stage 4 Tend is
  # required to commit and push the catalogs: that mandate exists
  # because 128 catalog entries once accumulated uncommitted and sat exposed to
  # a parallel-session branch-switch wipe. Blocking the push would restore that
  # bug, so the push is allowed and the irreversible verbs are denied instead —
  # force-push, delete, reset --hard, merge, publish. A blocked step fails safe:
  # the marker stays queued, and human PR/merge ratification is untouched.
  RUNLOG=$(mktemp "${TMPDIR:-/tmp}/recall-drain.XXXXXX")
  RECALL_WORKER=1 RECALL_SKILL_DIR="$SKILL_DIR" "$AGENT_PATH" \
    -p "/compound $SESSION" \
    --model "$MODEL" \
    --permission-mode auto \
    --settings "$PERMISSIONS" \
    > "$RUNLOG" 2>&1 &
  CHILD=$!

  # Poll rather than background a watchdog: a backgrounded `sleep` inherits this
  # script's stdout/stderr and holds them open after the run finishes, hanging
  # any caller reading those pipes.
  WAITED=0
  TIMED_OUT=0
  while kill -0 "$CHILD" 2>/dev/null && [ "$WAITED" -lt "$SESSION_TIMEOUT" ]; do
    sleep 1
    WAITED=$((WAITED + 1))
  done
  if kill -0 "$CHILD" 2>/dev/null; then
    log "timeout: killing $SESSION after ${WAITED}s"
    TIMED_OUT=1
    kill -TERM "$CHILD" 2>/dev/null
    sleep 5
    kill -KILL "$CHILD" 2>/dev/null
  fi
  wait "$CHILD" 2>/dev/null
  CHILD_RC=$?
  cat "$RUNLOG" >> "$LOG"

  AFTER=$(count_pending)
  if [ "$AFTER" -lt "$REMAINING" ]; then
    # The worker cleared its own marker. Still record the verdict -- the ledger
    # is what SessionEnd consults to avoid re-enqueueing this session later.
    record_processed "$SESSION" "cleared-by-worker" "$RUNLOG"
    rm -f "$ATTEMPTS_DIR/$SESSION" "$RUNLOG" "$AUTH_DOWN_MARKER" 2>/dev/null
  fi
  if [ "$AFTER" -ge "$REMAINING" ]; then
    # An auth/quota failure is not this session's fault, so it must not consume
    # one of its three attempts. That path is what emptied the queue into
    # quarantine while the health check still read "0 pending". Halt the run and
    # leave every marker exactly where it is.
    #
    # Two guards, because the first version had neither and cried outage on a
    # HEALTHY run: it grepped the whole child output, and the worker's own
    # report quoted the phrase "OAuth session expired" while describing a
    # session it had just distilled successfully. So (1) only look when the run
    # made no progress, and (2) anchor to line start — the CLI prints these as
    # bare lines, prose mentions them mid-sentence and in backticks.
    # The worker exited 0 without the command existing. Retrying cannot help
    # until the skill is installed, and burning attempts would quarantine
    # perfectly good sessions, so halt the run and leave every marker untouched.
    if [ "$CHILD_RC" -eq 0 ] && grep -qE "$WORKER_BROKEN_RE" "$RUNLOG"; then
      REASON=$(grep -Em1 "$WORKER_BROKEN_RE" "$RUNLOG")
      log "worker-broken: $REASON — the /compound skill is not reachable by $AGENT_PATH."
      log "  $SESSION left queued, no attempt consumed. Re-run install.sh, or check that"
      log "  the skill symlink still resolves and that RECALL_AGENT_BIN names the right CLI."
      rm -f "$RUNLOG"
      break
    fi
    # ORDER. The exit-code verdict is read BEFORE the outage scan. It used to be
    # the other way round, and since v3 moved marker-clearing into the drain,
    # "the run made no progress" became true of every SUCCESSFUL run too -- so
    # the only remaining guard was the ^ anchor. A worker that distilled a
    # session ABOUT auth or quota, and quoted one of those phrases at line
    # start, halted the queue, left its marker untouched (same mtime, so `ls
    # -tr` handed back the same one next tick) and consumed no attempt. The
    # queue wedged permanently behind it while SessionStart reported an outage.
    # The worker ran to completion and simply did not delete the marker. That is
    # the overwhelmingly common case and it is NOT a failure: a session can be
    # fully analysed and correctly yield nothing worth capturing ("0 new
    # artifacts, 0 routed -- DISCARDED"), and the worker then declines to clear
    # its own marker because SKILL.md tells it not to auto-clear. Unattended
    # there is nobody to give that approval, so 20 finished sessions stranded in
    # the queue burning retries toward quarantine.
    #
    # Marker lifecycle belongs to the drain, which handed the session out and
    # can see the structural signals the worker's prose cannot fake: exit code
    # and whether we had to kill it. Prose said "the skill clears markers it
    # processes" for three months and the skill did not.
    if [ "$TIMED_OUT" -eq 0 ] && [ "$CHILD_RC" -eq 0 ]; then
      if record_processed "$SESSION" "completed-marker-cleared-by-drain" "$RUNLOG"; then
        rm -f "$PENDING_DIR/$MARKER" "$ATTEMPTS_DIR/$SESSION" "$RUNLOG" "$AUTH_DOWN_MARKER" 2>/dev/null
        log "processed $SESSION — worker completed, verdict recorded, marker cleared by drain"
        DRAINED=$((DRAINED + 1))
        continue
      fi
      # Ledger write failed: keep the marker. Losing the session is worse than
      # processing it twice.
      log "warn: could not write processed record for $SESSION — marker left queued"
    fi

    # Reached only when the run did NOT exit 0 cleanly, which is what makes the
    # outage scan safe to run at all.
    if grep -qiE "$INFRA_RE" "$RUNLOG"; then
      REASON=$(grep -iEm1 "$INFRA_RE" "$RUNLOG")
      rm -f "$RUNLOG"
      printf '{"reason":%s,"detected_at":"%s","pending":%s}\n' \
        "$(printf '%s' "$REASON" | jq -Rs .)" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$(count_pending)" > "$AUTH_DOWN_MARKER"
      log "infra-down: $REASON — halting run, $SESSION left queued, no attempt consumed"
      break
    fi

    rm -f "$RUNLOG"
    # The marker survived a real failure (killed, or non-zero exit). The loop
    # always takes the oldest, so leaving it in
    # place unchanged means picking the same session forever and blocking every
    # newer marker behind it. But a failure is often transient: one 1.3 MB
    # transcript timed out twice and then distilled cleanly on the third
    # attempt, so quarantining on the first failure exiles healthy work into a
    # directory only a human empties. Count attempts, and send it to the back
    # of the queue in between by touching it — `ls -tr` orders by mtime.
    mkdir -p "$ATTEMPTS_DIR"
    N=$(( $(cat "$ATTEMPTS_DIR/$SESSION" 2>/dev/null || echo 0) + 1 ))
    echo "$N" > "$ATTEMPTS_DIR/$SESSION"
    if [ "$N" -lt "$MAX_ATTEMPTS" ]; then
      touch "$PENDING_DIR/$MARKER"
      log "no progress on $SESSION (attempt $N of $MAX_ATTEMPTS) — requeued for a later run"
      continue
    fi
    mkdir -p "$QUARANTINE_DIR"
    if mv "$PENDING_DIR/$MARKER" "$QUARANTINE_DIR/$MARKER" 2>/dev/null; then
      rm -f "$ATTEMPTS_DIR/$SESSION" 2>/dev/null
      log "quarantined $SESSION after $N attempts -> $QUARANTINE_DIR"
    else
      log "could not quarantine $SESSION — stopping to avoid a loop"
      break
    fi
    QUARANTINED=$(( QUARANTINED + 1 ))
    # Several failures in one run means something systemic, not one bad session.
    if [ "$QUARANTINED" -ge "$MAX_QUARANTINE" ]; then
      log "quarantined $QUARANTINED this run — stopping, check the log"
      break
    fi
    continue
  fi
  DRAINED=$(( DRAINED + REMAINING - AFTER ))
done

QMSG=""
[ "$QUARANTINED" -gt 0 ] && QMSG=", quarantined $QUARANTINED (requeue with: mv $QUARANTINE_DIR/*.json $PENDING_DIR/)"
log "done: drained $DRAINED, $(count_pending) remaining$QMSG"
exit 0
