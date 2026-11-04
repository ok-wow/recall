#!/usr/bin/env python3
"""Model-judged capture: scripts/recall_note.py, the inbox schema, and the SessionStart nudge."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import check, clean, env_for, finish, hook_sh, jsonl, py, tmp  # noqa: E402

import friction_events as fe  # noqa: E402

KINDS = ["failure", "process_failure", "decision", "success", "workflow", "guardrail", "policy"]


def events(h):
    return jsonl(h / "state" / "inbox" / "events.jsonl")


print("recall_note")
h = tmp()
env = env_for(h)
for k in KINDS:
    p = py("recall_note.py", k, f"one line about {k}", "--evidence", "src/a.py:10", "--confidence", "verified",
           "--session", "s-1", env=env)
    check(f"kind {k}: accepted, receipt says queued, exit 0",
          p.returncode == 0 and p.stdout.strip().startswith(f"recall: queued note ({k}, verified)"), p.stdout + p.stderr)
ev = events(h)
check("all seven notes are in the inbox with the schema fields",
      [e.get("kind") for e in ev] == KINDS
      and all(e["type"] == "note" and e["confidence"] == "verified" and e["session_id"] == "s-1"
              and e["host"] == "testhost" and e["evidence"] == {"pointer": "src/a.py:10"} and e["count"] == 1 and e["ts"]
              for e in ev), str(ev))

before = len(events(h))
for bad, why in ((["story", "text"], "unknown kind"), (["decision", "text", "--confidence", "sure"], "unknown confidence"),
                 (["decision", "   "], "empty text")):
    p = py("recall_note.py", *bad, env=env)
    check(f"{why}: rejected, nonzero, nothing written", p.returncode != 0 and len(events(h)) == before and "NOT queued" in p.stdout + p.stderr,
          p.stdout + p.stderr)

p = py("recall_note.py", "decision", "key is " + "gh" + "p_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0hJ3kL6", "--session", "s", env=env)
check("a credential in the note text is scrubbed before it is written",
      p.returncode == 0 and "Ab3dE6gH9j" not in str(events(h)), str(events(h)[-1]))

hb = tmp()
(hb / "state").mkdir()
(hb / "state" / "inbox").write_text("in the way")
p = py("recall_note.py", "decision", "x", "--session", "s", env=env_for(hb))
check("inbox unwritable: receipt says NOT queued and exit is nonzero",
      p.returncode != 0 and "NOT queued" in p.stdout + p.stderr and "queued note" not in p.stdout, p.stdout + p.stderr)
clean(hb)

# -- the schema (shared with detectors) --------------------------------------------------------------------------
base = {"signature": "s", "count": 1, "session_id": "x", "host": "h", "evidence": {"pointer": "p"}, "ts": "2026-11-01T00:00:00Z"}
for k in KINDS:
    check(f"schema accepts note kind {k}", fe.validate_event({**base, "type": "note", "kind": k, "text": "t", "confidence": "stated"}) == [])
check("schema rejects an unknown note kind", fe.validate_event({**base, "type": "note", "kind": "gossip", "text": "t", "confidence": "stated"}) != [])
check("schema rejects a note with no text", fe.validate_event({**base, "type": "note", "kind": "decision", "confidence": "stated"}) != [])
check("schema still accepts the detector types",
      all(fe.validate_event({**base, "type": t}) == [] for t in ("failed_command", "retry_loop", "user_correction", "named_pattern")))
check("schema rejects an unknown event type", fe.validate_event({**base, "type": "vibes"}) != [])

# -- SessionStart tells the model when to call it -------------------------------------------------------------------------------
p = hook_sh("recall-sessionstart.sh", {"hook_event_name": "SessionStart", "session_id": "sid-xyz"}, env)
out = p.stdout
check("SessionStart injects the nudge naming the CLI and this session id", "recall_note.py" in out and "sid-xyz" in out, out)
check("...for the three triggers: verified fix, decision with a rejected alternative, same correction twice",
      "verified" in out and "rejected alternative" in out and "twice" in out, out)
check("...and tells the model to say so in one line", "one line" in out, out)
check("...which lists all seven kinds", all(k in out for k in KINDS), out)
check("RECALL_SIGNALS=off removes the nudge",
      "recall_note.py" not in hook_sh("recall-sessionstart.sh", {"session_id": "s"}, env_for(h, RECALL_SIGNALS="off")).stdout)
clean(h)
sys.exit(finish("recall_note checks"))
