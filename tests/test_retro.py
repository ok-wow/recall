#!/usr/bin/env python3
"""recall_retro: cluster friction events across sessions; refuse to promote a one-off.

The events file is hand-written. A cluster seen in one session is a candidate
however loud it was; only a second session makes it confirmed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import check, clean, env_for, finish, jsonl, py, tmp  # noqa: E402


def ev(sid, sig, count, typ="retry_loop", ts="2026-11-01T00:00:00Z"):
    return {"type": typ, "signature": sig, "count": count, "session_id": sid, "host": "h",
            "evidence": {"path": f"/t/{sid}.jsonl", "line": 3}, "ts": ts}


h = tmp()
inbox = h / "state" / "inbox"
inbox.mkdir(parents=True)
rows = [
    ev("s1", "pytest <path> -x", 4), ev("s2", "pytest <path> -x", 3, ts="2026-11-02T00:00:00Z"),
    ev("s1", "alembic-multiple-heads", 1, "named_pattern"),
    ev("s3", "npm run build", 9, "failed_command"),
    # the same loop reported three times as it grew: one session, the largest count wins
    ev("s2", "git-conflict", 1, "named_pattern"), ev("s2", "git-conflict", 2, "named_pattern"), ev("s2", "git-conflict", 2, "named_pattern"),
    ev("s4", "git-conflict", 1, "named_pattern"),
]
(inbox / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
env = env_for(h)

print("recall_retro")
p = py("recall_retro.py", "--json", env=env)
rep = {c["signature"]: c for c in json.loads(p.stdout)["clusters"]}
check("exits 0 and ignores a corrupt line", p.returncode == 0 and len(rep) == 4, p.stdout + p.stderr)
c = rep["pytest <path> -x"]
check("a loop in two sessions is confirmed, with sessions, occurrences and a retries cost proxy",
      c["status"] == "confirmed" and c["sessions"] == 2 and c["occurrences"] == 7 and c["retries"] == 5, str(c))
check("a loud one-session cluster (9 failures) is still only a candidate",
      rep["npm run build"]["status"] == "candidate" and rep["npm run build"]["sessions"] == 1, str(rep["npm run build"]))
check("repeated reports of one session's event collapse to its largest count, not a sum",
      rep["git-conflict"]["occurrences"] == 3 and rep["git-conflict"]["sessions"] == 2, str(rep["git-conflict"]))
check("clusters are ordered by cost, highest first",
      [c["signature"] for c in json.loads(p.stdout)["clusters"]]
      == ["npm run build", "pytest <path> -x", "git-conflict", "alembic-multiple-heads"], p.stdout)

t = py("recall_retro.py", env=env).stdout
check("the text report labels candidate vs confirmed", "candidate" in t and "confirmed" in t, t)

p = py("recall_retro.py", "promote", "alembic-multiple-heads", env=env)
check("promote refuses a cluster seen in only one session",
      p.returncode != 0 and "refus" in (p.stdout + p.stderr).lower() and "1 session" in p.stdout + p.stderr, p.stdout + p.stderr)
check("...and writes nothing", not (inbox / "promoted.jsonl").exists())
p = py("recall_retro.py", "promote", "npm run build", env=env)
check("promote refuses even a 9-failure single-session cluster", p.returncode != 0 and not (inbox / "promoted.jsonl").exists())
p = py("recall_retro.py", "promote", "pytest <path> -x", env=env)
pr = jsonl(inbox / "promoted.jsonl")
check("promote accepts a confirmed cluster and records it", p.returncode == 0 and len(pr) == 1 and pr[0]["signature"] == "pytest <path> -x" and pr[0]["sessions"] == 2, p.stdout + p.stderr)
p = py("recall_retro.py", "promote", "no-such-signature", env=env)
check("promote of an unknown signature fails", p.returncode != 0)
clean(h)
sys.exit(finish("retro checks"))
