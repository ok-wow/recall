#!/usr/bin/env python3
"""fathom_backfill — pull every Fathom meeting into the connector index over REST.

The first pass at this ran through the Fathom MCP connector, which meant every
summary travelled through an agent session and landed in that session's saved
transcript. 182 of 201 records were left with no summary at all because the
per-record calls were too expensive to finish, so their entire searchable text
was "Meeting with <names>. Summary and action items are in Fathom." -- a pointer
with nothing to match a query against.

REST fixes both halves: the summaries never enter a model context, and a page of
50 costs one call instead of 50. Same shape as the Linear truncation repair
(53 calls, zero model tokens, 32s).

    python3 fathom_backfill.py                 # dry run, prints what would change
    python3 fathom_backfill.py --apply         # writes through connector_index
    python3 fathom_backfill.py --apply --since 2026-01-01

The key is read from $FATHOM_API_KEY, or from the `FATHOM_API_KEY=` line of
~/dev/ok-wow-ai/.env. It is never printed, never logged, and never passed on a
command line where `ps` would show it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import connector_index  # noqa: E402

API = "https://api.fathom.ai/external/v1/meetings"
SOURCE = "fathom"
# The API caps a page at 10 whatever you ask for, and allows 10 requests per 3
# seconds (ratelimit-limit: 10, ratelimit-reset: 3). Pacing below that is the
# difference between one clean drain and a 429 eight pages in.
PAGE = 10
RATE_DELAY = 0.45
ENV_FILE = Path.home() / "dev/ok-wow-ai/.env"
# connector_index caps the gist at 400. Leaving headroom means the sentence-
# boundary trim below never has to cut one short to satisfy the cap.
GIST_TARGET = 380
RETRY_ON = {408, 429, 500, 502, 503, 504, 529}


def load_key() -> str:
    key = os.environ.get("FATHOM_API_KEY", "").strip()
    if key:
        return key
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("FATHOM_API_KEY="):
                return line.split("=", 1)[1].strip()
    sys.exit(f"no FATHOM_API_KEY in the environment or {ENV_FILE}")


def get(url: str, key: str, tries: int = 5) -> dict:
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={"X-Api-Key": key})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_ON or attempt == tries - 1:
                # e carries the request in its repr; re-raise a clean message so
                # a traceback can never surface the header.
                raise SystemExit(f"Fathom returned HTTP {e.code}") from None
            # Fathom states its own wait. Guessing an exponential backoff when
            # the server has already answered the question just burns attempts.
            try:
                wait = float(e.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                wait = 0
            time.sleep(max(wait + 0.5, 2 ** attempt))
        except OSError as e:
            # URLError subclasses OSError, but a mid-stream ConnectionResetError
            # does not pass through urllib's wrapper at all -- catching only
            # URLError dropped the run on page ~40 of the first full drain.
            if attempt == tries - 1:
                raise SystemExit(f"Fathom unreachable: {type(e).__name__}") from None
            time.sleep(2 ** attempt)
    raise SystemExit("unreachable")


HEADING = re.compile(r"^\s*(#{1,6}\s|\*\*[^*]+\*\*\s*$|[-*]\s*$)")
BULLET = re.compile(r"^\s*[-*]\s+")
MD = re.compile(r"[*_`]+")


def gist_from(markdown: str, fallback: str) -> str:
    """First real prose of a summary, trimmed on a sentence boundary.

    An arbitrary character cut is what left 44% of the Linear descriptions
    ending mid-word; a boundary trim keeps the last sentence whole and still
    fills the budget.
    """
    prose: list[str] = []
    for raw in (markdown or "").splitlines():
        line = MD.sub("", BULLET.sub("", raw)).strip()
        if not line or HEADING.match(raw) or len(line) < 25:
            continue
        prose.append(line)
        if sum(len(p) for p in prose) > GIST_TARGET * 2:
            break
    text = " ".join(prose).strip()
    if not text:
        return fallback[:GIST_TARGET]
    if len(text) <= GIST_TARGET:
        return text
    cut = text[:GIST_TARGET]
    stop = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return (cut[: stop + 1] if stop > GIST_TARGET // 2 else cut.rsplit(" ", 1)[0]).strip()


def to_record(item: dict, keep_scope: str | None) -> dict | None:
    rid = item.get("recording_id")
    url = item.get("url") or item.get("share_url")
    date = (item.get("recording_start_time") or item.get("scheduled_start_time")
            or item.get("created_at") or "")[:10]
    title = (item.get("meeting_title") or item.get("title") or "").strip()
    if not (rid and url and date and title):
        return None

    people = []
    for inv in item.get("calendar_invitees") or []:
        n = (inv.get("name") or inv.get("email") or "").strip()
        if n:
            people.append(n)

    summary = (item.get("default_summary") or {}).get("markdown_formatted") or ""
    fallback = (f"Meeting with {', '.join(people)}." if people
                else "Meeting, no listed invitees.")
    rec = {
        "id": f"{SOURCE}:{rid}",
        "source": SOURCE,
        "title": title,
        "gist": gist_from(summary, fallback),
        "people": people,
        "where": item.get("meeting_type") or item.get("calendar_invitees_domains_type") or "",
        "date": date,
        "url": url,
        "synced": time.strftime("%Y-%m-%d"),
        "has_summary": bool(summary),
    }
    # Scoping is rescope_connector.py's job. Re-running this must not silently
    # reset a label that a scoring pass already paid for.
    if keep_scope:
        rec["scope"] = keep_scope
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    ap.add_argument("--since", metavar="YYYY-MM-DD", help="only meetings created on/after")
    ap.add_argument("--max-pages", type=int, default=200)
    args = ap.parse_args()

    key = load_key()
    existing = {r["id"]: r for r in connector_index.read(SOURCE)}
    print(f"index holds {len(existing)} {SOURCE} records")

    params = {"limit": str(PAGE), "include_summary": "true"}
    if args.since:
        params["created_after"] = f"{args.since}T00:00:00Z"

    def flush(batch: list[dict]) -> None:
        """Persist as we go. The first run fetched 70 meetings, hit a 429 on page
        eight and wrote none of them, because the only write was after the loop."""
        if args.apply and batch:
            connector_index.upsert(SOURCE, [{k: v for k, v in r.items()
                                             if k != "has_summary"} for r in batch])

    records, pending, cursor, pages = [], [], None, 0
    try:
        while pages < args.max_pages:
            q = dict(params)
            if cursor:
                q["cursor"] = cursor
            data = get(f"{API}?{urllib.parse.urlencode(q)}", key)
            items = data.get("items") or []
            for it in items:
                prev = existing.get(f"{SOURCE}:{it.get('recording_id')}")
                rec = to_record(it, (prev or {}).get("scope"))
                if rec:
                    records.append(rec)
                    pending.append(rec)
            pages += 1
            if pages % 10 == 0 or not data.get("next_cursor"):
                flush(pending)
                pending = []
                print(f"  page {pages}: {len(records)} usable so far"
                      + (" (written)" if args.apply else ""))
            cursor = data.get("next_cursor")
            if not cursor or not items:
                break
            time.sleep(RATE_DELAY)
    finally:
        flush(pending)

    with_summary = sum(1 for r in records if r["has_summary"])
    new = sum(1 for r in records if r["id"] not in existing)
    gained = sum(1 for r in records if r["has_summary"]
                 and not (existing.get(r["id"], {}).get("gist") or "").strip()
                 .endswith("Summary and action items are in Fathom."))
    print(f"\nfetched {len(records)} meetings over {pages} page(s)")
    print(f"  with a summary:      {with_summary}")
    print(f"  new to the index:    {new}")
    print(f"  already had a gist:  {gained}")

    if not args.apply:
        print("\ndry run — nothing written. Re-run with --apply")
        return 0

    print(f"\nindex now holds {len(connector_index.read(SOURCE))} {SOURCE} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
