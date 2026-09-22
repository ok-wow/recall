#!/usr/bin/env python3
"""connector_index — write and read the connector pointer index.

The decision this file ENFORCES (core-world-model, 2026-05-25): raw signal from
Slack, Gmail, meetings and the like is Orbit data, not Core. Core stores
distilled pointers, never bodies. That was a prose rule for four months and
prose rules get skipped, so it is a schema gate here instead: `gist` is capped,
and a record carrying a field that looks like a message body is refused.

One JSONL file per source under $RECALL_HOME/connectors/. One line per item:

    {"id": "slack:C083X9G32FN:1776818766.572109",
     "source": "slack", "title": "...", "gist": "...", "people": ["..."],
     "where": "#03_engineering", "date": "2026-04-21",
     "url": "https://...", "synced": "2026-09-21"}

Writing is idempotent and atomic: `upsert` replaces a record with the same id
rather than appending a second one, and the file is rewritten through a temp
file so a crashed sync cannot leave a half-line behind.

The collector is NOT here. Slack and Gmail are reachable only through MCP, which
means only from inside an agent session -- so this file takes records and does
not fetch them. That split is deliberate: the part that can be unit-tested has
no network, and the part that needs the network has no logic.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def env_path(*names_then_default) -> Path:
    *names, default = names_then_default
    for n in names:
        raw = os.environ.get(n)
        if raw:
            return Path(raw).expanduser()
    return Path(default).expanduser()


RECALL_HOME = env_path("RECALL_HOME", "OKWOW_HOME", Path.home() / ".recall")
CONNECTOR_DIR = env_path("RECALL_CONNECTOR_DIR", "OKWOW_CONNECTOR_DIR",
                         RECALL_HOME / "connectors")

REQUIRED = ("id", "source", "title", "date", "url")
# Same number recall.py truncates a body at. A gist longer than what a reader
# is ever shown is not a gist, it is a body with extra steps -- which is the
# exact thing this index exists not to hold.
GIST_CAP = 400
# Field names a well-meaning collector reaches for when it wants to "just keep
# the whole thing". Refused by name so the refusal is legible in the traceback.
BODY_FIELDS = frozenset({"body", "text", "content", "message", "transcript",
                         "raw", "full_text", "messages", "thread"})
SOURCE_OK = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")
# Scope is a LABEL, not a gate. The first design dropped anything the privacy
# axis flagged, which throws away a real record because of who it is about --
# "we agreed Alex owns onboarding" is organizational and also about a person.
# Labelling keeps it and lets the reader decide, and it is the taxonomy the
# compound router already uses: personal preference stays in the private store,
# team doctrine is shared, organization facts are the company's.
SCOPES = ("organization", "team", "preference", "personal", "unscoped")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


class RecordRejected(ValueError):
    """A record that would put a body in the index, or cannot be found again."""


def validate(rec: dict) -> dict:
    if not isinstance(rec, dict):
        raise RecordRejected("record is not an object")
    missing = [f for f in REQUIRED if not str(rec.get(f) or "").strip()]
    if missing:
        raise RecordRejected(f"missing required field(s): {', '.join(missing)}")
    if not SOURCE_OK.match(str(rec["source"])):
        raise RecordRejected(f"source {rec['source']!r} is not a short lowercase slug")
    if not ISO_DATE.match(str(rec["date"])):
        raise RecordRejected(f"date {rec['date']!r} is not YYYY-MM-DD")
    leaked = sorted(BODY_FIELDS & set(rec))
    if leaked:
        raise RecordRejected(
            f"field(s) {', '.join(leaked)} would store a body; put a <={GIST_CAP} "
            "char summary in `gist` and leave the body in the source app")
    scope = str(rec.get("scope") or "unscoped").strip().lower()
    if scope not in SCOPES:
        raise RecordRejected(f"scope {scope!r} is not one of {', '.join(SCOPES)}")
    gist = " ".join(str(rec.get("gist") or "").split())
    if len(gist) > GIST_CAP:
        raise RecordRejected(f"gist is {len(gist)} chars, cap is {GIST_CAP}")
    out = dict(rec)
    out["scope"] = scope
    out["gist"] = gist
    # A personal record's value is "a private conversation happened here, go
    # look" -- and who was in it is the sensitive half. Redacting the gist by
    # hand while leaving the names in `people` is redaction theatre: the store
    # is searchable, so the name is still an answer to a query. Dropping them
    # is structural so it cannot be forgotten on a record written at speed.
    if scope == "personal":
        rec = dict(rec, people=[])
    people = [] if scope == "personal" else out.get("people")
    out["people"] = [str(p) for p in people] if isinstance(people, list) else []
    return out


def path_for(source: str) -> Path:
    if not SOURCE_OK.match(str(source)):
        raise RecordRejected(f"source {source!r} is not a short lowercase slug")
    return CONNECTOR_DIR / f"{source}.jsonl"


def read(source: str) -> list[dict]:
    p = path_for(source)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            # One corrupt line must not cost the whole source. A sync that
            # crashed mid-write is the likeliest cause and the rest is fine.
            continue
        if isinstance(rec, dict) and rec.get("id"):
            out.append(rec)
    return out


def upsert(source: str, records: list[dict]) -> dict:
    """Merge records into a source file. Returns {added, updated, unchanged}.

    Every record is validated BEFORE anything is written, so a bad record in a
    batch of 200 fails the batch instead of leaving the file half-updated.
    """
    clean = [validate(r) for r in records]
    existing = {r["id"]: r for r in read(source)}
    order = [r["id"] for r in read(source)]
    added = updated = unchanged = 0
    for r in clean:
        rid = r["id"]
        if rid not in existing:
            existing[rid] = r
            order.append(rid)
            added += 1
        elif existing[rid] != r:
            # Keep `synced` moving but do not reorder: position is age.
            existing[rid] = r
            updated += 1
        else:
            unchanged += 1
    p = path_for(source)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(existing[i], ensure_ascii=False) + "\n"
                           for i in order))
    os.replace(tmp, p)          # atomic: readers never see a partial file
    return {"added": added, "updated": updated, "unchanged": unchanged,
            "total": len(order)}


def sources() -> list[str]:
    if not CONNECTOR_DIR.exists():
        return []
    return sorted(p.stem for p in CONNECTOR_DIR.glob("*.jsonl"))


def main() -> int:
    """Read JSON records on stdin (a list, or one object per line) and upsert.

    Used by a collector session: it gathers through MCP, shapes the records,
    and pipes them here, so the shaping logic and the gate live in one place
    rather than being re-implemented per source.
    """
    if len(sys.argv) < 2:
        sys.stderr.write("usage: connector_index.py <source> [--stats]\n")
        return 2
    source = sys.argv[1]
    if "--stats" in sys.argv:
        for s in sources():
            print(f"  {s:<12} {len(read(s)):>6} records  {path_for(s)}")
        return 0
    raw = sys.stdin.read().strip()
    if not raw:
        sys.stderr.write("connector_index: nothing on stdin\n")
        return 2
    try:
        data = json.loads(raw)
        records = data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        records = [json.loads(l) for l in raw.splitlines() if l.strip()]
    try:
        res = upsert(source, records)
    except RecordRejected as exc:
        sys.stderr.write(f"connector_index: REFUSED — {exc}\n")
        return 1
    print(f"ok  {source}  +{res['added']} added  ~{res['updated']} updated  "
          f"={res['unchanged']} unchanged  {res['total']} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
