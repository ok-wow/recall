#!/usr/bin/env python3
"""The backfill's two pure halves: turning a markdown summary into a gist, and
turning an API item into a record.

Both were written against defects that had already been paid for. An arbitrary
character cut left 44% of the Linear descriptions ending mid-word, so the trim
here has to land on a boundary. And a re-run must not reset a scope that a
scoring pass spent tokens producing, so `to_record` takes the previous scope
rather than dropping it."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import fathom_backfill as fb  # noqa: E402

fails: list[str] = []
ran = [0]


def check(name, ok, detail=None):
    ran[0] += 1
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}" + ("" if ok else f"  {detail!r}"))
    if not ok:
        fails.append(name)


ITEM = {
    "recording_id": 42,
    "meeting_title": "Design Review",
    "url": "https://fathom.video/calls/42",
    "recording_start_time": "2026-03-04T17:00:00Z",
    "calendar_invitees": [{"name": "Ada"}, {"email": "b@x.com"}],
    "default_summary": {"markdown_formatted": ""},
}


def main() -> int:
    # --- gist_from -------------------------------------------------------
    md = "## Notes\n" + ("The team agreed the vault listing needs its own width. " * 20)
    g = fb.gist_from(md, "fallback")
    check("gist stays inside the writer's cap", len(g) <= 400, len(g))
    check("gist fills most of its budget", len(g) > fb.GIST_TARGET * 0.6, len(g))
    check("gist drops the heading", not g.startswith("#"), g[:20])
    check("gist never ends mid-word", not g.endswith(("agree", "wid", "ow")), g[-12:])
    check("gist ends on a boundary", g[-1] in ".?!" or " " not in g[-2:], g[-6:])

    check("markdown emphasis is stripped",
          "*" not in fb.gist_from("**Bold** " + "a decision was reached here. " * 12, "f"))
    check("bullet markers are stripped",
          not fb.gist_from("- " + "a decision was reached here. " * 12, "f").startswith("-"))

    check("an empty summary falls back", fb.gist_from("", "Meeting with Ada.") == "Meeting with Ada.")
    check("a heading-only summary falls back", fb.gist_from("# Title\n## Sub", "F.") == "F.")
    # A short summary is not padded and not cut.
    short = "The pricing tier for Business was settled at per-seat this quarter."
    check("a short summary survives whole", fb.gist_from(short, "f") == short)

    # --- to_record -------------------------------------------------------
    r = fb.to_record(dict(ITEM), None)
    check("id is source-prefixed", r["id"] == "fathom:42", r["id"])
    check("date is the recording day", r["date"] == "2026-03-04", r["date"])
    check("an invitee without a name falls back to the email",
          r["people"] == ["Ada", "b@x.com"], r["people"])
    check("no summary means the invitee fallback",
          r["gist"] == "Meeting with Ada, b@x.com.", r["gist"])
    check("a record with no scope carries none", "scope" not in r, r.get("scope"))

    # The re-run guarantee. Losing this silently discards a paid scoring pass.
    r2 = fb.to_record(dict(ITEM), "team")
    check("a previous scope is preserved", r2["scope"] == "team", r2.get("scope"))

    # --- the writer accepts what this produces ---------------------------
    import connector_index as ci
    ok = True
    try:
        ci.validate(r2)
    except ci.RecordRejected as e:
        ok = str(e)
    check("the record passes the index's own gate", ok is True, ok)

    long_gist = dict(r2, gist=fb.gist_from(md, "f"))
    try:
        ci.validate(long_gist)
        ok = True
    except ci.RecordRejected as e:
        ok = str(e)
    check("a full-length gist still passes the cap", ok is True, ok)

    # --- required fields --------------------------------------------------
    for field in ("recording_id", "url", "meeting_title"):
        broken = {k: v for k, v in ITEM.items() if k != field}
        if field == "meeting_title":
            broken.pop("title", None)
        check(f"a record missing {field} is dropped", fb.to_record(broken, None) is None)

    check("the key is never taken from argv",
          "--key" not in Path(fb.__file__).read_text())

    print()
    if fails:
        print(f"FAIL — {len(fails)}/{ran[0]} checks red: {', '.join(fails)}")
        return 1
    print(f"PASS — {ran[0]}/{ran[0]} checks green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
