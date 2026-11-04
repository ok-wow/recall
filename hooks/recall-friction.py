#!/usr/bin/env python3
"""Recall friction hook: PostToolUse and Stop (and SessionEnd, through recall-sessionend.sh).

    PostToolUse (shell tools)  scan the lines added since the last call; record new friction
                               events to $RECALL_HOME/inbox/events.jsonl. Prints nothing.
    Stop                       the same, flush counts, and print at most ONE user-visible
                               line as hook JSON {"systemMessage": "..."} when something was
                               recorded or judged worth recording this turn. Nothing otherwise.
    --end MARKER               SessionEnd: attach the tally to the pending marker, print the
                               one-line receipt (called by recall-sessionend.sh after the
                               marker exists, so it never reports a queue that did not happen).

Fail open, always: any problem is swallowed and the hook exits 0 inside its 10 s
budget. No model is called. RECALL_SIGNALS=off|quiet|on (default on) governs what
Stop may say; recording is unaffected. See scripts/friction_events.py for the labels.
"""
import json
import os
import signal
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("RECALL_SKILL_DIR") or os.path.join(HERE, "..", "scripts"))

BUDGET_SECONDS = 8
SHELL_TOOLS = {"bash", "shell", "exec_command", "local_shell", "shell_command", "container.exec"}


def _timeout(signum, frame):
    raise TimeoutError("hook budget spent")


def run() -> None:
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    if not isinstance(payload, dict):
        return
    sid = payload.get("session_id")
    transcript = payload.get("transcript_path")
    if not sid or not transcript:
        return

    import friction_events as fe

    home = fe.recall_home()
    if len(sys.argv) >= 3 and sys.argv[1] == "--end":
        try:
            print(fe.end_session(home, str(sid), transcript, sys.argv[2]))
        except BaseException as e:  # the marker exists, so say that much and no more
            print(f"recall: queued session {str(sid)[:8]} for distillation; friction/sync receipt incomplete ({type(e).__name__})")
        return
    event = payload.get("hook_event_name")
    if event == "PostToolUse":
        if str(payload.get("tool_name", "")).lower() in SHELL_TOOLS:
            fe.process(home, str(sid), transcript)
        return
    if event != "Stop":
        return
    sync = fe.light_sync(home, str(sid), transcript) if fe.sync_enabled() else None
    out = fe.process(home, str(sid), transcript, announce=True, sync_fn=sync)
    if fe.sync_enabled():
        fe.maybe_checkpoint(home, str(sid), transcript)
    if out["message"]:
        print(json.dumps({"systemMessage": out["message"]}))


def main() -> int:
    try:
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(BUDGET_SECONDS)
    except (ValueError, AttributeError):
        pass
    try:
        run()
    except BaseException:  # a hook must never fail a session, a timeout included
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
