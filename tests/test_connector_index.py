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

        # --- scope labels, it does not redact ----------------------------
        # Clearing `people` on a personal record was measured and did not
        # redact: 7 of 10 such records still named someone in their title or
        # gist, while the routing value was gone. Privacy here is an egress
        # rule, not a schema one -- so every scope keeps its names.
        v = ci.validate(rec(scope="personal", people=["A Person", "Another"]))
        check("a personal record keeps its participant names",
              v["people"] == ["A Person", "Another"], v["people"])
        v = ci.validate(rec(scope="team", people=["A Person"]))
        check("a non-personal record keeps them", v["people"] == ["A Person"], v["people"])
        ci.upsert("slack", [rec(id="slack:p1", scope="personal", people=["X"])])
        got = [r for r in ci.read("slack") if r["id"] == "slack:p1"][0]
        check("and they survive the round trip to disk",
              got.get("people") == ["X"], got.get("people"))

        # --- isolation: sources are separate files -----------------------
        ci.upsert("gmail", [rec(id="gmail:abc", source="gmail",
                                url="https://mail.google.com/x")])
        check("a second source writes its own file",
              sorted(ci.sources()) == ["gmail", "slack"], ci.sources())
        # 2 originals + the personal record written by the scope check above.
        check("and does not disturb the first", len(ci.read("slack")) == 3,
              len(ci.read("slack")))

        # --- suppression: removed means removed, including on re-sync ----
        # The owner removes a record; the next sync fetches it again. Hiding it
        # in one reader would not survive that, so every path checks the list.
        ci.upsert("fathom", [rec(id="fathom:1", source="fathom", title="Keep"),
                             rec(id="fathom:2", source="fathom", title="Remove me")])
        gone = ci.suppress("fathom", ["fathom:2"], "personal-finance", "2026-09-22")
        check("suppress returns the removed record for safekeeping",
              [r["id"] for r in gone] == ["fathom:2"], gone)
        check("the record is gone from the file on disk",
              "fathom:2" not in ci.path_for("fathom").read_text())
        check("the other record is untouched", [r["id"] for r in ci.read("fathom")] == ["fathom:1"])
        r5 = ci.upsert("fathom", [rec(id="fathom:2", source="fathom", title="Remove me")])
        check("a re-sync cannot bring it back",
              r5["suppressed"] == 1 and "fathom:2" not in ci.path_for("fathom").read_text(), r5)
        lst = (tmp / "fathom.suppressed").read_text()
        check("the suppression list holds the id and a category, no content",
              lst.strip() == "fathom:2\t2026-09-22\tpersonal-finance" and "Remove me" not in lst, lst)
        ci.suppress("fathom", ["fathom:2"], "personal-finance", "2026-09-23")
        check("suppressing twice does not duplicate the list entry",
              (tmp / "fathom.suppressed").read_text().count("fathom:2") == 1)
        p = ci.path_for("fathom")
        p.write_text(p.read_text() + json.dumps(rec(id="fathom:2", source="fathom")) + "\n")
        check("a record written around the index is still hidden on read",
              "fathom:2" not in [r["id"] for r in ci.read("fathom")])
        try:
            ci.suppress("fathom", ["fathom:1"], "their mortgage call with the bank", "2026-09-22")
            ok = False
        except ci.RecordRejected:
            ok = True
        check("a reason that is text, not a category, is refused", ok)

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
