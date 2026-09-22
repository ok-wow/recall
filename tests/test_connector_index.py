#!/usr/bin/env python3
"""The connector index: does the gate actually refuse a body, and is it safe to
re-run? Both questions are structural — a sync runs on a clock and a collector
is an agent, so neither "remember not to store bodies" nor "remember not to
double-append" is a control that survives contact."""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

fails: list[str] = []
ran = [0]


def check(name, ok, detail=None):
    ran[0] += 1
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}" + ("" if ok else f"  {detail!r}"))
    if not ok:
        fails.append(name)


def fresh_module(tmp: Path):
    """Re-import with CONNECTOR_DIR pointed at a temp dir — the module reads it
    at import time, same as recall.py does with its paths."""
    os.environ["RECALL_CONNECTOR_DIR"] = str(tmp)
    import connector_index
    return importlib.reload(connector_index)


def rec(**kw):
    base = {"id": "slack:C1:1", "source": "slack", "title": "A thread",
            "gist": "Someone asked a question.", "people": ["Shalin Amin"],
            "where": "#03_engineering", "date": "2026-04-21",
            "url": "https://okwow.slack.com/archives/C1/p1"}
    base.update(kw)
    return base


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ci = fresh_module(tmp)

        # --- the gate: bodies never land ---------------------------------
        for field in ("body", "text", "transcript", "messages"):
            try:
                ci.validate(rec(**{field: "the entire thread verbatim"}))
                ok = False
            except ci.RecordRejected as exc:
                ok = field in str(exc)
            check(f"a `{field}` field is refused", ok)

        try:
            ci.validate(rec(gist="x" * (ci.GIST_CAP + 1)))
            ok = False
        except ci.RecordRejected as exc:
            ok = "cap" in str(exc)
        check("a gist over the cap is refused", ok)
        check("a gist at exactly the cap is kept",
              ci.validate(rec(gist="x" * ci.GIST_CAP))["gist"] == "x" * ci.GIST_CAP)
        check("the cap equals what recall actually displays", ci.GIST_CAP == 400)

        # --- the gate: a record you cannot find again is not a pointer ----
        for field in ("id", "url", "date", "title"):
            try:
                ci.validate(rec(**{field: ""}))
                ok = False
            except ci.RecordRejected as exc:
                ok = field in str(exc)
            check(f"a record with no `{field}` is refused", ok)
        try:
            ci.validate(rec(date="April 21"))
            ok = False
        except ci.RecordRejected:
            ok = True
        check("a non-ISO date is refused", ok)

        # --- idempotence: a sync runs on a clock -------------------------
        r1 = ci.upsert("slack", [rec(), rec(id="slack:C1:2", title="Another")])
        check("first sync adds both", (r1["added"], r1["total"]) == (2, 2), r1)
        r2 = ci.upsert("slack", [rec(), rec(id="slack:C1:2", title="Another")])
        check("re-running the same batch adds nothing",
              (r2["added"], r2["updated"], r2["total"]) == (0, 0, 2), r2)
        check("the file still has exactly two lines",
              len(ci.path_for("slack").read_text().strip().splitlines()) == 2)

        r3 = ci.upsert("slack", [rec(gist="The gist got better.")])
        check("a changed record updates in place",
              (r3["added"], r3["updated"], r3["total"]) == (0, 1, 2), r3)
        check("the new gist is what reads back",
              [x for x in ci.read("slack") if x["id"] == "slack:C1:1"][0]["gist"]
              == "The gist got better.")
        check("order is stable — position is age",
              [x["id"] for x in ci.read("slack")] == ["slack:C1:1", "slack:C1:2"])

        # --- a bad record fails the BATCH, never half of it --------------
        before = ci.path_for("slack").read_text()
        try:
            ci.upsert("slack", [rec(id="slack:C1:3", title="Good"),
                                rec(id="slack:C1:4", body="leaked")])
            ok = False
        except ci.RecordRejected:
            ok = True
        check("one bad record rejects the whole batch", ok)
        check("and the file is untouched", ci.path_for("slack").read_text() == before)

        # --- a corrupt line costs one record, not the source -------------
        p = ci.path_for("slack")
        p.write_text(p.read_text() + "{not json\n")
        check("a corrupt line is skipped, the rest still read",
              len(ci.read("slack")) == 2)

        # --- personal scope drops names, structurally --------------------
        v = ci.validate(rec(scope="personal", people=["A Person", "Another"]))
        check("a personal record keeps no participant names", v["people"] == [], v["people"])
        v = ci.validate(rec(scope="team", people=["A Person"]))
        check("a non-personal record keeps them", v["people"] == ["A Person"], v["people"])
        ci.upsert("slack", [rec(id="slack:p1", scope="personal", people=["X"])])
        got = [r for r in ci.read("slack") if r["id"] == "slack:p1"][0]
        check("and the names are absent on disk, not just in memory",
              got.get("people") == [], got.get("people"))

        # --- isolation: sources are separate files -----------------------
        ci.upsert("gmail", [rec(id="gmail:abc", source="gmail",
                                url="https://mail.google.com/x")])
        check("a second source writes its own file",
              sorted(ci.sources()) == ["gmail", "slack"], ci.sources())
        # 2 originals + the personal record written by the scope check above.
        check("and does not disturb the first", len(ci.read("slack")) == 3,
              len(ci.read("slack")))

        # --- the temp file never survives a write ------------------------
        check("no .tmp left behind", not list(tmp.glob("*.tmp")))

    print()
    if fails:
        print(f"FAIL — {len(fails)}/{ran[0]} checks red: {', '.join(fails)}")
        return 1
    print(f"PASS — {ran[0]}/{ran[0]} checks green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
