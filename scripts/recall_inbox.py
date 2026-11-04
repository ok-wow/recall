#!/usr/bin/env python3
"""Bring cloud sessions home: fetch branch inbox/cloud and queue each session once.

    recall_inbox.py ingest

Run by the drain before it counts the queue. For every inbox/<host>/<sid>.json on
the fetched branch it either queues the session or says why not:

  - already consumed (tracked by session id in $RECALL_HOME/inbox/consumed.json): skipped,
    so a session is ingested exactly once however many times the drain runs;
  - a checkpoint ("final": false) younger than RECALL_INBOX_STALE_HOURS (default 6): left
    for the final file, not consumed;
  - malformed, or still holding something shaped like a credential: parked in
    quarantine/_inbox/ (the reason, never the content) and consumed;
  - otherwise: its events join the local inbox (deduped), its digest is written to
    digests/<sid>.md, and a pending marker pointing at it is written, so the drain's own
    oversize, retry and quarantine logic carries it from there.

It only reads the remote. It never pushes. The remote is `origin` of $RECALL_HOME
(RECALL_INBOX_REMOTE overrides); with no personal repo there is nothing to do.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import friction_events as fe  # noqa: E402
import personal_repo  # noqa: E402
import receipts  # noqa: E402
import scrub  # noqa: E402

REF_BRANCH = "inbox/cloud"
FILE_RE = re.compile(r"^inbox/([^/]+)/([^/]+)\.json$")
FETCH_TIMEOUT = 30


def _git(home: Path, *args: str, timeout: int = FETCH_TIMEOUT):
    return subprocess.run(["git", "-C", str(home), *args], capture_output=True, timeout=timeout,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _load(p: Path) -> dict:
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(p: Path, data: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(data)
    os.replace(tmp, p)


def _age_hours(ts) -> float:
    try:
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600
    except (TypeError, ValueError):
        return 1e9


def _quarantine(home: Path, sid: str, host: str, reason: str, consumed: dict) -> None:
    _write(home / "quarantine" / "_inbox" / f"{sid}.json",
           json.dumps({"session_id": sid, "host": host, "reason": reason, "at": fe.now_iso()}))
    consumed[sid] = {"quarantined": reason, "at": fe.now_iso()}
    receipts.log(home, "inbox_quarantined", session_id=sid, host=host, reason=reason)
    print(f"inbox: quarantined {sid}: {reason}")


def _ingest_one(home: Path, sid: str, host: str, payload: dict, consumed: dict) -> None:
    known = {(e["session_id"], e["type"], e["signature"], e.get("count")) for e in fe.read_events(home)}
    fresh = [e for e in payload.get("events") or []
             if isinstance(e, dict) and not fe.validate_event(e)
             and (e["session_id"], e["type"], e["signature"], e.get("count")) not in known]
    if fresh:
        fe.append_events(home, fresh)
    digest = payload.get("digest")
    queued = False
    if digest and not (home / "processed" / f"{sid}.json").exists() and not (home / "pending" / f"{sid}.json").exists():
        dpath = home / "digests" / f"{sid}.md"
        _write(dpath, digest)
        marker = {"session_id": sid, "transcript_path": str(dpath), "ended_at": payload.get("updated_at") or fe.now_iso(),
                  "transcript_size_bytes": len(digest.encode()), "reason": "cloud-inbox", "host": host,
                  "friction": payload.get("friction") or {}}
        _write(home / "pending" / f"{sid}.json", json.dumps(marker, indent=2))
        queued = True
    consumed[sid] = {"ingested_at": fe.now_iso(), "host": host, "queued": queued, "events": len(fresh)}
    receipts.log(home, "ingested", session_id=sid, host=host, queued=queued, events=len(fresh))
    print(f"inbox: ingested {sid} from {host} ({len(fresh)} event(s), {'queued' if queued else 'no digest to queue'})")


def ingest(home: Path) -> int:
    if not personal_repo.is_own_repo(home):
        return 0
    remote = os.environ.get("RECALL_INBOX_REMOTE") or "origin"
    if remote not in personal_repo.remotes(home):
        return 0
    ref = f"refs/remotes/{remote}/{REF_BRANCH}"
    try:
        f = _git(home, "fetch", "-q", remote, f"+refs/heads/{REF_BRANCH}:{ref}")
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"inbox: fetch failed ({type(e).__name__})")
        return 0
    if f.returncode != 0:
        if b"couldn't find remote ref" not in f.stderr:
            print("inbox: fetch failed: " + (f.stderr.decode(errors="replace").strip().splitlines() or ["?"])[-1])
        return 0
    ls = _git(home, "ls-tree", "-r", "--name-only", ref, "--", "inbox/")
    if ls.returncode != 0:
        return 0
    stale = float(os.environ.get("RECALL_INBOX_STALE_HOURS") or 6)
    cpath = home / "inbox" / "consumed.json"
    consumed = _load(cpath)
    for rel in ls.stdout.decode().splitlines():
        m = FILE_RE.match(rel)
        if not m or m.group(2) in consumed:
            continue
        host, sid = m.groups()
        raw = _git(home, "show", f"{ref}:{rel}").stdout.decode(errors="replace")
        try:
            payload = json.loads(raw)
            assert isinstance(payload, dict) and payload.get("schema") == 1 and payload.get("session_id") == sid
        except (ValueError, AssertionError):
            _quarantine(home, sid, host, "malformed or wrong schema", consumed)
            continue
        left = scrub.residue(raw)
        if left:
            _quarantine(home, sid, host, "still credential-shaped: " + ", ".join(left), consumed)
            continue
        if not payload.get("final") and _age_hours(payload.get("updated_at")) < stale:
            continue
        _ingest_one(home, sid, host, payload, consumed)
    _write(cpath, json.dumps(consumed, indent=1))
    return 0


def main(argv: list) -> int:
    if argv[:1] != ["ingest"]:
        sys.stderr.write(__doc__ or "")
        return 2
    try:
        return ingest(fe.recall_home())
    except Exception as e:  # the drain must go on without us
        print(f"inbox: ingest failed ({type(e).__name__}: {e})")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
