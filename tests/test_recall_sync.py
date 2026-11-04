#!/usr/bin/env python3
"""recall_sync: a scrubbed digest + friction tally goes to inbox/<host>/<sid>.json on branch inbox/cloud.

The remote is a local bare repo standing in for recall-personal, so the real git
push path runs; only GitHub itself is out of reach (see the PR's "not verified").
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import FIX, bare_remote, cc_session, check, clean, env_for, finish, git, jsonl, py, tmp  # noqa: E402

import recall_sync as rs  # noqa: E402

SID = "5e55105e-0000-4000-8000-000000000001"
TOKEN = "gh" + "p_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0hJ3kL6"


def sync(h, env, *extra, sid=SID, t=None):
    return py("recall_sync.py", "--session", sid, "--transcript", str(t or h / "t.jsonl"), "--final", *extra, env=env)


def payload(rem, sid=SID, host="testhost"):
    return json.loads(git(rem, "show", f"inbox/cloud:inbox/{host}/{sid}.json"))


def secretive(h) -> Path:
    t = cc_session(h / "t.jsonl", 4, extra=[
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": f"my token is {TOKEN} ok"}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "s0", "is_error": True,
                                                                  "content": "Exit code 1\nAPI_TOKEN=abc123def456 rejected"}]}},
    ])
    return t


print("recall_sync")

# -- happy path, to a dedicated branch -----------------------------------------------------------------------------
h = tmp()
rem = bare_remote(h)
main_before = git(rem, "rev-parse", "main")
t = secretive(h)
env = env_for(h, RECALL_PERSONAL_REMOTE=str(rem))
p = sync(h, env)
check("sync: receipt says synced and names the branch", p.returncode == 0 and "recall: synced" in p.stdout and "inbox/cloud" in p.stdout, p.stdout + p.stderr)
check("sync: the file is at inbox/<host>/<sid>.json on branch inbox/cloud", f"inbox/testhost/{SID}.json" in git(rem, "ls-tree", "-r", "--name-only", "inbox/cloud"))
check("sync: main is untouched", git(rem, "rev-parse", "main") == main_before and "inbox" not in git(rem, "ls-tree", "-r", "--name-only", "main"))
check("sync: the branch shares no history with main (a dedicated branch)", main_before not in git(rem, "rev-list", "inbox/cloud").split())
pl = payload(rem)
check("payload: schema, ids, final flag", pl["schema"] == 1 and pl["session_id"] == SID and pl["host"] == "testhost" and pl["final"] is True, str(pl)[:300])
check("payload: carries the digest", "step 0" in (pl["digest"] or ""), str(pl)[:300])
check("payload: carries the friction tally", "events" in pl["friction"], str(pl["friction"]))
raw = git(rem, "show", f"inbox/cloud:inbox/testhost/{SID}.json")
check("payload: neither the token nor the .env value is anywhere in what was pushed", TOKEN not in raw and "abc123def456" not in raw, raw[:200])
rev1 = git(rem, "rev-parse", "inbox/cloud")
time.sleep(1.1)  # cross a second boundary: a timestamp alone must never count as "new"
sync(h, env)
check("sync: running it again with nothing new adds no commit", git(rem, "rev-parse", "inbox/cloud") == rev1)
t2 = cc_session(h / "t2.jsonl", 5)
sync(h, env, sid="other-session", t=t2)
check("sync: a second session adds its file and keeps the first",
      {f"inbox/testhost/{SID}.json", "inbox/testhost/other-session.json"} <= set(git(rem, "ls-tree", "-r", "--name-only", "inbox/cloud").split()))

# turn notes travel in the same payload, and a light sync keeps the digest already built
py("recall_note.py", "decision", "chose jsonl over sqlite", "--session", SID, "--confidence", "stated", env=env)
sync(h, env, "--no-digest")
pl = payload(rem)
check("notes: turn-level notes ride the same payload", any(e.get("kind") == "decision" for e in pl["events"]), str(pl["events"]))
check("light sync: keeps the digest from the earlier full sync", "step 0" in (pl["digest"] or ""))
clean(h)

# -- it never pushes to the default branch ---------------------------------------------------------------------------------
h = tmp()
rem = bare_remote(h)
main_before = git(rem, "rev-parse", "main")
secretive(h)
for b in ("main", "master"):
    p = sync(h, env_for(h, RECALL_PERSONAL_REMOTE=str(rem)), "--branch", b)
    check(f"--branch {b}: refused and says so", "refused" in p.stdout and "recall: synced" not in p.stdout, p.stdout)
check("...and main did not move, nor did any branch appear",
      git(rem, "rev-parse", "main") == main_before
      and git(rem, "for-each-ref", "--format=%(refname:short)", "refs/heads").split() == ["main"], git(rem, "branch", "--list"))
clean(h)

# -- degrade: nothing durable, nothing lost silently --------------------------------------------------------------------------
h = tmp()
secretive(h)
env = env_for(h)
p = sync(h, env)
out = h / "state" / "outbox" / "testhost" / f"{SID}.json"
check("no remote: receipt says exactly what was not synced and why",
      "NOT synced" in p.stdout and "no remote configured" in p.stdout and SID[:8] in p.stdout, p.stdout)
check("no remote: the scrubbed digest is kept locally", out.exists() and TOKEN not in out.read_text() and "step 0" in out.read_text())
st = json.loads((h / "state" / "sync-state.json").read_text())
check("no remote: sync-state lists it as unsynced with the reason", "no remote" in st["unsynced"][SID]["reason"], str(st))

env = env_for(h, RECALL_PERSONAL_REMOTE=str(h / "missing.git"))
p = sync(h, env)
check("unreachable remote: NOT synced, with a reason, exit 0", p.returncode == 0 and "NOT synced" in p.stdout, p.stdout + p.stderr)

env = env_for(h, RECALL_PERSONAL_REMOTE="https://github.com/someone/recall-personal.git")
p = sync(h, env)
check("github remote with no credential: NOT synced, names the missing credential, no attempt",
      "NOT synced" in p.stdout and "credential" in p.stdout, p.stdout)

rem = bare_remote(h)
p = sync(h, env_for(h, RECALL_PERSONAL_REMOTE=str(rem)))
st = json.loads((h / "state" / "sync-state.json").read_text())
check("once the remote works the same session syncs and leaves the unsynced list", "recall: synced" in p.stdout and SID not in st.get("unsynced", {}), p.stdout + str(st))
clean(h)

# -- fail closed on scrubbing ------------------------------------------------------------------------------------------------------
h = tmp()
rem = bare_remote(h)
t = cc_session(h / "t.jsonl", 4, extra=[{"type": "user", "message": {"role": "user", "content": [
    {"type": "text", "text": "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\nand it never ends"}]}}])
p = sync(h, env_for(h, RECALL_PERSONAL_REMOTE=str(rem)))
check("uncertain scrub: receipt says nothing was uploaded and why",
      "NOT synced" in p.stdout and "scrubbing uncertain" in p.stdout, p.stdout)
check("uncertain scrub: nothing on the remote, nothing written locally",
      "inbox/cloud" not in git(rem, "branch", "--list") and not (h / "state" / "outbox").exists(), git(rem, "branch", "-a"))
clean(h)

# -- the checkpoint rule (size/time) ---------------------------------------------------------------------------------------------------------
NOW, B, S = 1_000_000.0, 512 * 1024, 600
table = [
    ({}, 10, False), ({"first_seen": NOW - 4000}, 10, True), ({}, 2_000_000, True),
    ({"last_synced_at": NOW - 10, "last_size": 1000}, 1500, False),
    ({"last_synced_at": NOW - 4000, "last_size": 1000}, 1000, False),
    ({"last_synced_at": NOW - 4000, "last_size": 1000}, 1100, True),
]
for entry, size, want in table:
    check(f"checkpoint_due({entry}, size={size}) == {want}", rs.checkpoint_due(entry, size, NOW, B, S) is want)
sys.exit(finish("recall_sync checks"))
