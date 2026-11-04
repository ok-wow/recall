#!/usr/bin/env python3
"""Cloud -> local: the real drain fetches branch inbox/cloud and ingests each session exactly once.

Both ends are real: recall_sync.py writes from a "cloud" RECALL_HOME to a bare
repo; recall-drain.sh runs in a "local" RECALL_HOME whose store is a git repo
with that bare repo as origin. The worker is a stub that exits 0.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import HOOKS, bare_remote, cc_session, check, clean, env_for, finish, git, jsonl, py, run, tmp  # noqa: E402

TOKEN = "gh" + "p_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0hJ3kL6"
FINAL, LIVE, BAD, POISON = "f1nal000-0000-4000-8000-000000000001", "1ive0000-0000-4000-8000-000000000002", "bad00000-0", "p0is0n00-0"


def cloud_sync(root: Path, rem: Path, sid: str, final: bool, transcript: Path):
    c = root / "cloud"
    c.mkdir(exist_ok=True)
    env = env_for(c, RECALL_HOST="cloudbox", RECALL_PERSONAL_REMOTE=str(rem))
    py("recall_note.py", "decision", f"note from {sid[:4]}", "--session", sid, "--confidence", "stated", env=env)
    args = ["--final"] if final else []
    return py("recall_sync.py", "--session", sid, "--transcript", str(transcript), *args, env=env)


def push_raw(root: Path, rem: Path, rel: str, text: str):
    w = root / ("rawclone-" + Path(rel).stem)
    subprocess.run(["git", "clone", "-q", "-b", "inbox/cloud", str(rem), str(w)], check=True, capture_output=True)
    (w / rel).parent.mkdir(parents=True, exist_ok=True)
    (w / rel).write_text(text)
    for a in (["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "raw"], ["push", "-q", "origin", "inbox/cloud"]):
        subprocess.run(["git", "-C", str(w), *a], check=True, capture_output=True)


def drain(root: Path, local: Path, stale="6"):
    counter = root / "runs"
    stub = root / "agent"
    stub.write_text(f"#!/bin/sh\necho run >> {counter}\nexit 0\n")
    stub.chmod(0o755)
    (root / "perms.json").write_text('{"permissions":{"deny":["Bash(rm -rf:*)"]}}')
    e = env_for(local, RECALL_AGENT_BIN=str(stub), RECALL_DRAIN_PERMISSIONS=str(root / "perms.json"),
                RECALL_DRAIN_BUDGET="30", RECALL_INBOX_STALE_HOURS=stale)
    run(["bash", str(HOOKS / "recall-drain.sh")], e, timeout=120)
    return len(counter.read_text().splitlines()) if counter.exists() else 0


print("inbox ingest")
root = tmp()
rem = bare_remote(root)
t = cc_session(root / "t.jsonl", 4)
cloud_sync(root, rem, FINAL, True, t)
cloud_sync(root, rem, LIVE, False, t)
push_raw(root, rem, f"inbox/cloudbox/{BAD}.json", "{ this is not json")
push_raw(root, rem, f"inbox/cloudbox/{POISON}.json", json.dumps({
    "schema": 1, "session_id": POISON, "host": "cloudbox", "final": True, "updated_at": "2026-11-01T00:00:00Z",
    "digest": "leaked " + TOKEN + " " * 3000, "friction": {"events": []}, "events": []}))
main_before = git(rem, "rev-parse", "main")
branch_before = git(rem, "rev-parse", "inbox/cloud")

local = root / "local"
(local / "state").mkdir(parents=True)
for a in (["init", "-q", "-b", "main"], ["remote", "add", "origin", str(rem)]):
    subprocess.run(["git", "-C", str(local / "state"), *a], check=True, capture_output=True)

runs = drain(root, local)
s = local / "state"
check("drain ran the worker once, for the final cloud session only", runs == 1, f"runs={runs}")
check("the cloud session was distilled and recorded in the ledger", (s / "processed" / f"{FINAL}.json").exists())
check("its digest is on local disk where the worker was pointed", "step 0" in (s / "digests" / f"{FINAL}.md").read_text()
      if (s / "digests" / f"{FINAL}.md").exists() else False)
consumed = json.loads((s / "inbox" / "consumed.json").read_text()) if (s / "inbox" / "consumed.json").exists() else {}
check("ingested sessions are marked consumed by session id", FINAL in consumed and LIVE not in consumed, str(consumed))
check("a still-live (non-final, recent) checkpoint is NOT ingested yet", not (s / "pending" / f"{LIVE}.json").exists() and not (s / "processed" / f"{LIVE}.json").exists())
check("a malformed inbox file is quarantined, not ingested", (s / "quarantine" / "_inbox").is_dir() and any(BAD in f.name for f in (s / "quarantine" / "_inbox").iterdir()))
check("a file that still holds a credential-shaped string is quarantined, never ingested",
      any(POISON in f.name for f in (s / "quarantine" / "_inbox").iterdir()) and not (s / "digests" / f"{POISON}.md").exists())
check("the cloud session's turn notes reached the local inbox",
      any(e.get("kind") == "decision" and e["session_id"] == FINAL for e in jsonl(s / "inbox" / "events.jsonl")))
ing = [r for r in jsonl(s / "receipts.jsonl") if r.get("event") == "ingested"]
check("an ingested receipt names the cloud host and session", len(ing) == 1 and ing[0]["session_id"] == FINAL and ing[0].get("host") == "cloudbox", str(ing))

runs = drain(root, local)
check("exactly once: a second drain does not run the worker again", runs == 1, f"runs={runs}")
check("exactly once: the session's events are not duplicated",
      len([e for e in jsonl(s / "inbox" / "events.jsonl") if e.get("kind") == "decision" and e["session_id"] == FINAL]) == 1)
check("the local side never wrote to the remote", git(rem, "rev-parse", "main") == main_before and git(rem, "rev-parse", "inbox/cloud") == branch_before)

runs = drain(root, local, stale="0")
check("a checkpoint whose container went quiet (stale) is ingested as final", runs == 2 and LIVE in json.loads((s / "inbox" / "consumed.json").read_text()), f"runs={runs}")

c = root / "cloud"
late_env = env_for(c, RECALL_HOST="cloudbox", RECALL_PERSONAL_REMOTE=str(rem))
py("recall_note.py", "decision", "the last turns of a long session", "--session", LIVE, "--confidence", "stated", env=late_env)
py("recall_sync.py", "--session", LIVE, "--transcript", str(t), "--final", env=late_env)
runs = drain(root, local)
check("a final file arriving after its stale checkpoint still delivers its new events",
      any(e.get("kind") == "decision" and e["session_id"] == LIVE and "last turns" in json.dumps(e)
          for e in jsonl(s / "inbox" / "events.jsonl")))
check("the late final does not run the worker a second time for that session", runs == 2, f"runs={runs}")
clean(root)
sys.exit(finish("inbox ingest checks"))
