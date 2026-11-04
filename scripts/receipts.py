#!/usr/bin/env python3
"""Receipts: a record of what Recall did, and the few lines SessionStart shows for it.

    $RECALL_HOME/receipts.jsonl    one event per line: queued, distilled, ingested, synced, sync_failed

    receipts.py snapshot                      print the catalogs' entry ids as JSON
    receipts.py distilled SESSION BEFORE      the drain calls this after a run: record what ENTERED
                                              the catalogs since BEFORE (a snapshot file)
    receipts.py summary                       the SessionStart block; prints nothing when there is nothing

A receipt claims only what was observed. `distilled` is a diff of the catalogs
taken around the drain's run, so a pre-existing entry is never credited to a
session, and a run that added nothing says so. If the "before" snapshot is
missing, the receipt says what it kept is unknown rather than guessing.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

KINDS = {"FAILURE_MODES": ("failure_modes", "failure mode"),
         "PROCESS_FAILURES": ("process_failures", "process failure"),
         "DECISIONS": ("decisions", "decision")}
ID_LINE = re.compile(r"^\s*-?\s*id:\s*['\"]?([^\s'\"#]+)", re.M)
LIMIT = 5
IDS_SHOWN = 3


def home() -> Path:
    raw = os.environ.get("RECALL_HOME") or os.environ.get("OKWOW_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".recall"


def catalog_dir() -> Path:
    return Path(os.environ.get("RECALL_CATALOG_DIR") or home() / "catalogs")


def log(h: Path, event: str, **fields) -> None:
    from datetime import datetime, timezone
    row = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "event": event, **fields}
    try:
        h.mkdir(parents=True, exist_ok=True)
        with open(h / "receipts.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError:
        pass


def snapshot(cdir: Path | None = None) -> dict:
    cdir = cdir or catalog_dir()
    out = {}
    for name, (key, _) in KINDS.items():
        try:
            ids = ID_LINE.findall((cdir / f"{name}.yaml").read_text(errors="replace"))
        except OSError:
            ids = []
        out[key] = ids
    return out


def diff(before: dict, after: dict) -> dict:
    return {key: [i for i in after.get(key, []) if i not in set(before.get(key, []))] for key, _ in KINDS.values()}


def distilled(session: str, before_path: str) -> None:
    h = home()
    try:
        before = json.loads(Path(before_path).read_text())
        captured = diff(before, snapshot())
    except (OSError, ValueError):
        captured = None
    rec = h / "processed" / f"{session}.json"
    if captured is not None:
        try:
            d = json.loads(rec.read_text())
            d["captured"] = captured
            tmp = rec.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=2))
            os.replace(tmp, rec)
        except (OSError, ValueError):
            pass
    log(h, "distilled", session_id=session, captured=captured)


# -- the SessionStart block ----------------------------------------------------------------
def _count(d: Path, pattern: str = "*.json") -> int:
    try:
        return sum(1 for _ in d.glob(pattern))
    except OSError:
        return 0


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _session_line(row: dict) -> str:
    sid = str(row.get("session_id", "?"))[:8]
    cap = row.get("captured")
    if cap is None:
        return f"  {sid}  distilled; what it kept is unknown (no before-snapshot)"
    parts = []
    for _, (key, noun) in KINDS.items():
        ids = cap.get(key) or []
        if ids:
            shown = ", ".join(ids[:IDS_SHOWN]) + (f", +{len(ids) - IDS_SHOWN}" if len(ids) > IDS_SHOWN else "")
            parts.append(f"{_plural(len(ids), noun)} ({shown})")
    return f"  {sid}  " + (", ".join(parts) if parts else "nothing worth keeping")


def summary(h: Path | None = None) -> str:
    h = h or home()
    rows = []
    try:
        for line in (h / "receipts.jsonl").read_text(errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("event") == "distilled":
                rows.append(r)
    except OSError:
        pass
    lines = []
    seen = []
    for r in reversed(rows):
        if r.get("session_id") not in seen:
            seen.append(r.get("session_id"))
            lines.append(_session_line(r))
        if len(seen) >= LIMIT:
            break
    lines.reverse()
    pending = _count(h / "pending")
    if pending:
        lines.append(f"  {pending} queued, not yet distilled (the drain runs on its schedule)")
    stuck = _count(h / "quarantine") + _count(h / "quarantine" / "_oversized")
    if stuck:
        lines.append(f"  {stuck} stuck in quarantine or parked as oversized, not distilled")
    inbox_q = _count(h / "quarantine" / "_inbox")
    if inbox_q:
        lines.append(f"  {inbox_q} cloud inbox file(s) quarantined, not ingested (malformed or credential-shaped)")
    try:
        unsynced = json.loads((h / "sync-state.json").read_text()).get("unsynced") or {}
    except (OSError, ValueError):
        unsynced = {}
    if unsynced:
        first = next(iter(unsynced.values())).get("reason", "unknown reason")
        lines.append(f"  NOT synced: {len(unsynced)} digest(s) kept locally only — {first}")
    if not lines:
        return ""
    return "\n".join(["recall — what the last sessions kept:"] + lines[: 11]) + "\n"


def main(argv: list) -> int:
    cmd = argv[0] if argv else "summary"
    try:
        if cmd == "snapshot":
            print(json.dumps(snapshot()))
        elif cmd == "distilled" and len(argv) == 3:
            distilled(argv[1], argv[2])
        elif cmd == "summary":
            sys.stdout.write(summary())
        else:
            sys.stderr.write(__doc__ or "")
            return 2
    except Exception as e:  # a receipt must never break the thing it reports on
        sys.stderr.write(f"receipts: {type(e).__name__}: {e}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
