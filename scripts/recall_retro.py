#!/usr/bin/env python3
"""Cluster friction events across sessions, and refuse to promote a one-off.

    recall_retro.py [report] [--json]      clusters by signature, most expensive first
    recall_retro.py promote SIGNATURE      record a confirmed cluster in inbox/promoted.jsonl

Reads $RECALL_HOME/inbox/events.jsonl. A cluster is every event with one
signature, one count per session (a session's repeated reports collapse to its
largest). Per cluster: sessions, occurrences, and retries (count - 1 per
session) as the cost proxy.

A cluster seen in only ONE session is a `candidate` however loud: ten failures in
one afternoon is one sighting. It becomes `confirmed` at a second session, and
`promote` refuses anything not confirmed. Promotion only records the decision;
turning it into a catalog entry stays with /compound.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import friction_events as fe  # noqa: E402

MIN_SESSIONS = 2


def clusters(home: Path) -> list:
    by: dict = {}
    for ev in fe.collapse(fe.read_events(home)):
        by.setdefault(ev["signature"], []).append(ev)
    out = []
    for sig, evs in by.items():
        counts = [int(e.get("count", 1)) for e in evs]
        sessions = len({e["session_id"] for e in evs})
        out.append({
            "signature": sig, "type": max(evs, key=lambda e: fe.TYPE_RANK.get(e.get("type"), 0)).get("type"),
            "sessions": sessions, "occurrences": sum(counts), "retries": sum(c - 1 for c in counts),
            "status": "confirmed" if sessions >= MIN_SESSIONS else "candidate",
            "evidence": [e.get("evidence") for e in evs][:3],
        })
    out.sort(key=lambda c: (-c["retries"], -c["occurrences"], c["signature"]))
    return out


def report(cl: list) -> str:
    if not cl:
        return "recall retro: no friction events recorded yet.\n"
    lines = ["status      sessions  occurrences  retries  signature"]
    for c in cl:
        lines.append(f"{c['status']:<10}  {c['sessions']:>8}  {c['occurrences']:>11}  {c['retries']:>7}  {c['signature']}")
    n = sum(1 for c in cl if c["status"] == "candidate")
    lines.append(f"\n{n} candidate(s) seen in only one session: not promotable until a second session hits them.")
    return "\n".join(lines) + "\n"


def promote(home: Path, sig: str) -> int:
    match = [c for c in clusters(home) if c["signature"] == sig]
    if not match:
        print(f"recall retro: no cluster named {sig!r}")
        return 1
    c = match[0]
    if c["status"] != "confirmed":
        print(f"recall retro: refusing to promote {sig!r}: seen in {c['sessions']} session (needs {MIN_SESSIONS}); it stays a candidate")
        return 1
    row = {"ts": fe.now_iso(), "signature": sig, "sessions": c["sessions"], "occurrences": c["occurrences"],
           "retries": c["retries"], "evidence": c["evidence"]}
    p = fe.events_path(home).with_name("promoted.jsonl")
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    print(f"recall retro: promoted {sig!r} ({c['sessions']} sessions, {c['retries']} retries)")
    return 0


def main(argv: list) -> int:
    home = fe.recall_home()
    if argv and argv[0] == "promote":
        if len(argv) != 2:
            sys.stderr.write("usage: recall_retro.py promote SIGNATURE\n")
            return 2
        return promote(home, argv[1])
    cl = clusters(home)
    if "--json" in argv:
        print(json.dumps({"clusters": cl}, indent=2))
    else:
        sys.stdout.write(report(cl))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
