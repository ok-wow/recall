#!/usr/bin/env python3
"""Turn-level capture with visible signals: hooks/recall-friction.py.

Stop prints one user-visible line (hook JSON `systemMessage`) when something was
recorded or judged worth recording, and nothing otherwise. The three labels are
literal states: candidate (detected, NOT written), queued (written to the
inbox), synced (pushed to inbox/cloud). The word "captured" is never used.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import FIX, bare_remote, check, clean, env_for, finish, git, hook_py, jsonl, tmp  # noqa: E402

SID = "sess-0001"


def stop(env, transcript, sid=SID):
    return hook_py("recall-friction.py", {"hook_event_name": "Stop", "session_id": sid,
                                          "transcript_path": str(transcript)}, env)


def post(env, transcript, sid=SID, tool="Bash"):
    return hook_py("recall-friction.py", {"hook_event_name": "PostToolUse", "session_id": sid, "tool_name": tool,
                                          "transcript_path": str(transcript), "tool_response": {"stdout": "", "stderr": ""}}, env)


def msg(p):
    return json.loads(p.stdout)["systemMessage"] if p.stdout.strip() else None


def events(h):
    return jsonl(h / "state" / "inbox" / "events.jsonl")


def setup(fixture=None):
    h = tmp()
    t = h / f"{SID}.jsonl"
    if fixture:
        shutil.copy(FIX / fixture, t)
    return h, t


print("signals")

# -- a quiet turn emits nothing, at all ---------------------------------------------------------
h, t = setup("claude_quiet.jsonl")
p = stop(env_for(h), t)
check("a turn with nothing notable: exit 0, empty stdout", p.returncode == 0 and p.stdout == "", repr(p.stdout) + p.stderr)
check("...and nothing was written", events(h) == [])
clean(h)

# -- one signal per turn, deduped per signature per session ------------------------------------------
h, t = setup("claude_patterns.jsonl")
env = env_for(h)
p = stop(env, t)
m = msg(p)
check("a notable turn emits a systemMessage", p.returncode == 0 and m is not None, p.stdout + p.stderr)
check("...one line, labelled queued, naming the thing, saying it is unconfirmed",
      m and "\n" not in m and m.startswith("Recall: queued") and "alembic-multiple-heads" in m and "unconfirmed" in m, str(m))
check("...four things were found, one is shown, the rest are counted", m and "+3 more" in m, str(m))
check("...the word 'captured' is never used", m and "captured" not in m.lower(), str(m))
ev = events(h)
check("all four were written to the inbox with the full field set",
      len(ev) == 4 and all({"type", "signature", "count", "session_id", "host", "evidence", "ts"} <= set(e) for e in ev), str(ev))
check("...host and session are the real ones", {e["host"] for e in ev} == {"testhost"} and {e["session_id"] for e in ev} == {SID})
seen = [m]
for _ in range(3):
    seen.append(msg(stop(env, t)))
sigs = [next((s for s in ("alembic-multiple-heads", "git-conflict", "ci-check-failed:ci-python", "linter-agents-reviewed-date") if s in (x or "")), None) for x in seen]
check("one signal per turn: the next three turns announce the other three, each exactly once",
      sorted(s for s in sigs if s) == sorted(["alembic-multiple-heads", "git-conflict", "ci-check-failed:ci-python", "linter-agents-reviewed-date"]) and len(set(sigs)) == 4, str(seen))
check("dedupe per signature: a fifth turn is silent", stop(env, t).stdout == "")
clean(h)

# -- incremental cursor: no double count across turns -----------------------------------------------------
h, _ = setup()
t = h / f"{SID}.jsonl"
lines = (FIX / "claude_loop.jsonl").read_text().splitlines(keepends=True)
env = env_for(h)
t.write_text("".join(lines[:5]))
stop(env, t)
t.write_text("".join(lines[:9]))
stop(env, t)
stop(env, t)
t.write_text("".join(lines))
stop(env, t)
rows = [e for e in events(h) if e["type"] == "retry_loop"]
check("cursor: a 4-run loop spread over turns ends as count 4, never 6 or 8",
      rows and max(e["count"] for e in rows) == 4 and rows[-1]["count"] == 4, str(events(h)))
check("cursor: an unchanged turn appended nothing",
      len(events(h)) == len({(e["type"], e["signature"], e["count"]) for e in events(h)}), str(events(h)))
check("cursor: state records where it stopped", any((h / "state" / "friction-state").glob("*.json")))
clean(h)

# -- PostToolUse records as it happens, defers count growth to Stop -------------------------------------------------
h, _ = setup()
t = h / f"{SID}.jsonl"
env = env_for(h)
t.write_text("".join(lines[:5]))
p = post(env, t)
check("PostToolUse(Bash): records the failure, prints nothing", p.returncode == 0 and p.stdout == "" and len(events(h)) == 1, str(events(h)) + p.stdout)
t.write_text("".join(lines[:9]))
post(env, t)
check("PostToolUse: the failed command escalates to a retry_loop event",
      [e["type"] for e in events(h)] == ["failed_command", "retry_loop"], str(events(h)))
t.write_text("".join(lines))
n = len(events(h))
post(env, t)
check("PostToolUse: further runs of a known loop add no line (count growth waits for Stop)", len(events(h)) == n)
stop(env, t)
check("Stop flushes the final count", events(h)[-1]["type"] == "retry_loop" and events(h)[-1]["count"] == 4, str(events(h)))
h2, t2 = setup("claude_patterns.jsonl")
post(env_for(h2), t2, tool="Read")
check("PostToolUse for a non-shell tool is ignored", events(h2) == [])
clean(h, h2)

# -- recurrence: a known signature from an earlier session is notable even when small ------------------------------------------
h, t = setup("claude_two_fails.jsonl")
env = env_for(h)
check("a lone failed command, first time ever, is recorded but not announced", stop(env, t).stdout == "" and len(events(h)) == 1)
clean(h)
h, t = setup("claude_two_fails.jsonl")
env = env_for(h)
inbox = h / "state" / "inbox"
inbox.mkdir(parents=True)
from datetime import datetime, timedelta, timezone  # noqa: E402
two_days = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
(inbox / "events.jsonl").write_text(json.dumps({"type": "failed_command", "signature": "npm run build", "count": 1,
                                               "session_id": "older", "host": "x", "evidence": {"path": "p", "line": 1}, "ts": two_days}) + "\n")
m = msg(stop(env, t))
check("a second sighting this week is announced as such", m and "2nd time this week" in m and "npm run build" in m, str(m))
clean(h)

# -- noise controls -------------------------------------------------------------------------------------------------------------
h, t = setup("claude_patterns.jsonl")
p = stop(env_for(h, RECALL_SIGNALS="off"), t)
check("RECALL_SIGNALS=off: no output, but the inbox still got the events", p.stdout == "" and len(events(h)) == 4, p.stdout)
clean(h)
h, t = setup("claude_patterns.jsonl")
check("RECALL_SIGNALS=quiet: first sightings stay silent", stop(env_for(h, RECALL_SIGNALS="quiet"), t).stdout == "")
clean(h)
h, t = setup("claude_patterns.jsonl")
inbox = h / "state" / "inbox"
inbox.mkdir(parents=True)
(inbox / "events.jsonl").write_text(json.dumps({"type": "named_pattern", "signature": "git-conflict", "count": 1, "session_id": "older",
                                               "host": "x", "evidence": {"path": "p", "line": 1}, "ts": two_days}) + "\n")
m = msg(stop(env_for(h, RECALL_SIGNALS="quiet"), t))
check("RECALL_SIGNALS=quiet: a recurrence is announced", m and "git-conflict" in m and "2nd time" in m, str(m))
clean(h)

# -- labels tell the truth ------------------------------------------------------------------------------------------------------------
h, t = setup("claude_patterns.jsonl")
(h / "state").mkdir()
(h / "state" / "inbox").write_text("a file where the inbox directory should be")
p = stop(env_for(h), t)
m = msg(p)
check("write failed: the label is candidate and says it was NOT written",
      p.returncode == 0 and m and m.startswith("Recall: candidate") and "not written" in m and "queued" not in m, str(m) + p.stderr)
clean(h)

h, t = setup("claude_patterns.jsonl")
rem = bare_remote(h)
m = msg(stop(env_for(h, RECALL_SYNC="1", RECALL_PERSONAL_REMOTE=str(rem)), t))
check("pushed to inbox/cloud: the label is synced", m and m.startswith("Recall: synced"), str(m))
check("...and the push really landed on inbox/cloud, not main",
      "inbox/testhost/" + SID + ".json" in git(rem, "ls-tree", "-r", "--name-only", "inbox/cloud")
      and git(rem, "ls-tree", "-r", "--name-only", "main") == "README", git(rem, "branch", "-a"))
clean(h)

h, t = setup("claude_patterns.jsonl")
m = msg(stop(env_for(h, RECALL_SYNC="1", RECALL_PERSONAL_REMOTE=str(h / "no-such-remote.git")), t))
check("sync enabled but the push failed: it stays queued and says not synced",
      m and m.startswith("Recall: queued") and "not synced" in m and "synced —" not in m, str(m))
check("...and the event is still in the inbox", len(events(h)) == 4)
clean(h)

# -- fail open -----------------------------------------------------------------------------------------------------------------------------
h = tmp()
check("garbage stdin: exit 0, no output", hook_py("recall-friction.py", {}, env_for(h)).returncode == 0)
p = stop(env_for(h), h / "missing.jsonl")
check("missing transcript: exit 0, no output", p.returncode == 0 and p.stdout == "", p.stdout + p.stderr)
clean(h)
sys.exit(finish("signal checks"))
