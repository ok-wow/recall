#!/usr/bin/env python3
"""Visible receipts: SessionStart says what was kept, SessionEnd says what was queued.

The state files below are hand-written to the documented receipt format. The
point of every case is the same: never claim a capture that did not happen, and
stay silent only when there is nothing to say.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import FIX, HOOKS, cc_session, check, clean, env_for, finish, hook_sh, jsonl, run, tmp  # noqa: E402

A, B, C = "aaaaaaaa-1111-2222-3333-444444444444", "bbbbbbbb-1111-2222-3333-444444444444", "cccccccc-1111-2222-3333-444444444444"


def start(home, **extra):
    return hook_sh("recall-sessionstart.sh", {"hook_event_name": "SessionStart", "session_id": "new-session"},
                   env_for(home, **extra))


def write_receipts(state: Path, rows: list) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "receipts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


print("receipts")

# -- silence only when there is genuinely nothing ---------------------------------
h = tmp()
p = start(h)
check("empty state: no receipt block", "recall — " not in p.stdout and "queued" not in p.stdout, p.stdout)
check("empty state, signals off: SessionStart prints nothing at all", start(h, RECALL_SIGNALS="off").stdout.strip() == "")
clean(h)

# -- a populated state ------------------------------------------------------------------------
h = tmp()
s = h / "state"
write_receipts(s, [
    {"ts": "2026-11-01T10:00:00Z", "event": "distilled", "session_id": A,
     "captured": {"failure_modes": ["alembic-heads-trap"], "process_failures": [], "decisions": ["use-jsonl-inbox"]}},
    {"ts": "2026-11-02T10:00:00Z", "event": "distilled", "session_id": B, "captured": {}},
])
(s / "pending").mkdir()
(s / "pending" / f"{C}.json").write_text(json.dumps({"session_id": C, "transcript_path": "/x", "ended_at": "2026-11-03T00:00:00Z"}))
(s / "quarantine" / "_inbox").mkdir(parents=True)
(s / "quarantine" / "_inbox" / "bad.json").write_text("{}")
(s / "sync-state.json").write_text(json.dumps({"unsynced": {"dddddddd-0": {"reason": "no credential for github.com", "at": "2026-11-03T01:00:00Z"}}}))
p = start(h)
out = p.stdout
check("SessionStart exits 0", p.returncode == 0, p.stderr)
check("receipt block is at the top of the context", out.lstrip().startswith("recall — "), out[:120])
la = [l for l in out.splitlines() if "aaaaaaaa" in l]
check("a distilled session lists counts by kind and the entry ids",
      la and "1 failure mode" in la[0] and "1 decision" in la[0] and "alembic-heads-trap" in out and "use-jsonl-inbox" in out, str(la))
lb = [l for l in out.splitlines() if "bbbbbbbb" in l]
check("a session that kept nothing says so and claims no entries",
      lb and "nothing" in lb[0] and "failure mode" not in lb[0] and "decision" not in lb[0], str(lb))
lc = [l for l in out.splitlines() if "queued" in l and "not yet" in l]
check("a pending marker is reported as queued, not as captured", lc and "1" in lc[0] and "kept" not in lc[0], str(lc))
check("cloud inbox files in quarantine are counted", "quarantine" in out and "1 cloud" in out, out)
check("an unsynced digest is reported with its reason", "NOT synced" in out and "no credential for github.com" in out, out)
block = out.split("<system-reminder>")[0]
check("the receipt block is under 12 lines", len([l for l in block.splitlines() if l.strip()]) <= 12, block)

many = [{"ts": f"2026-10-{d:02d}T10:00:00Z", "event": "distilled", "session_id": f"{d:08d}-x",
         "captured": {"decisions": [f"d-{d}"]}} for d in range(1, 29)]
write_receipts(s, many)
block = start(h).stdout.split("<system-reminder>")[0]
check("28 receipts still print at most 12 lines", len([l for l in block.splitlines() if l.strip()]) <= 12, block)
check("only the most recent sessions are listed", "00000028" in block and "00000001" not in block, block)
clean(h)

# -- SessionEnd: a one-line receipt of what was queued ---------------------------------------------
h = tmp()
env = env_for(h)
t = cc_session(h / "t.jsonl", 4)
p = hook_sh("recall-sessionend.sh", {"session_id": A, "transcript_path": str(t)}, env)
said = p.stdout + p.stderr
check("SessionEnd queues a substantive session", (h / "state" / "pending" / f"{A}.json").exists(), said)
check("SessionEnd prints one receipt line naming the session", said.count("recall: queued") == 1 and "aaaaaaaa" in said, said)
rows = [r for r in jsonl(h / "state" / "receipts.jsonl") if r.get("event") == "queued"]
check("SessionEnd records the same receipt", len(rows) == 1 and rows[0]["session_id"] == A, str(rows))

thin = h / "thin.jsonl"
thin.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "ok"}]}}) + "\n")
p = hook_sh("recall-sessionend.sh", {"session_id": B, "transcript_path": str(thin)}, env)
check("a session that is not queued produces no 'queued' claim",
      "queued" not in p.stdout + p.stderr and not (h / "state" / "pending" / f"{B}.json").exists(), p.stdout + p.stderr)

loop = h / "loop.jsonl"
shutil.copy(FIX / "claude_loop.jsonl", loop)
p = hook_sh("recall-sessionend.sh", {"session_id": C, "transcript_path": str(loop)}, env)
marker = json.loads((h / "state" / "pending" / f"{C}.json").read_text())
fr = marker.get("friction", {})
check("the per-session friction tally is attached to the pending marker",
      [(e["type"], e["count"]) for e in fr.get("events", [])] == [("retry_loop", 4)], str(marker))
check("the SessionEnd receipt mentions the friction it recorded", "friction" in (p.stdout + p.stderr), p.stdout + p.stderr)
clean(h)

# -- the drain writes the receipt from what it actually changed --------------------------------------------
DRAIN = HOOKS / "recall-drain.sh"


def drain(adds_entry: bool) -> tuple:
    h = tmp()
    s = h / "state"
    (s / "pending").mkdir(parents=True)
    (s / "catalogs").mkdir()
    (s / "catalogs" / "FAILURE_MODES.yaml").write_text("- id: old-one\n  symptom: x\n")
    (s / "pending" / f"{A}.json").write_text(json.dumps({"session_id": A, "transcript_path": str(h / "t"), "transcript_size_bytes": 1000}))
    stub = h / "agent"
    body = "printf -- '- id: new-trap\\n  symptom: y\\n' >> \"$RECALL_CATALOG_DIR/FAILURE_MODES.yaml\"\n" if adds_entry else ""
    stub.write_text("#!/bin/sh\n" + body + "exit 0\n")
    stub.chmod(0o755)
    (h / "perms.json").write_text('{"permissions":{"deny":["Bash(rm -rf:*)"]}}')
    e = env_for(h, RECALL_AGENT_BIN=str(stub), RECALL_DRAIN_PERMISSIONS=str(h / "perms.json"), RECALL_DRAIN_BUDGET="30")
    run(["bash", str(DRAIN)], e, timeout=90)
    rec = json.loads((s / "processed" / f"{A}.json").read_text()) if (s / "processed" / f"{A}.json").exists() else {}
    return h, rec, [r for r in jsonl(s / "receipts.jsonl") if r.get("event") == "distilled"]


h, rec, rows = drain(True)
check("drain: the ledger records exactly the entry ids that appeared during the run",
      rec.get("captured") == {"failure_modes": ["new-trap"], "process_failures": [], "decisions": []}, str(rec))
check("drain: a distilled receipt is logged for the session", len(rows) == 1 and rows[0]["captured"]["failure_modes"] == ["new-trap"], str(rows))
clean(h)
h, rec, rows = drain(False)
check("drain: a run that added nothing records an EMPTY capture, not a pre-existing entry",
      rec.get("captured") == {"failure_modes": [], "process_failures": [], "decisions": []}, str(rec))
check("drain: and its receipt claims nothing", len(rows) == 1 and not any(rows[0]["captured"].values()), str(rows))
clean(h)

sys.exit(finish("receipt checks"))
