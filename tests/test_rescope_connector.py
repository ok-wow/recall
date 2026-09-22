#!/usr/bin/env python3
"""The collector's decide-half, with a fake scorer standing in for the gateway.

A real Jev call cannot be in a suite that has to pass in a fresh clone with no
key, so the scorer is injected. What is asserted is the logic around it: that a
dry run writes nothing, that argmax is taken across scopes rather than against a
threshold, and that batching does not drop or reorder a record."""
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


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        os.environ["RECALL_CONNECTOR_DIR"] = td
        import connector_index as ci
        importlib.reload(ci)
        import rescope_connector as rc
        importlib.reload(rc)

        base = {"source": "fathom", "where": "fathom", "date": "2026-01-01",
                "url": "https://fathom.video/calls/1", "gist": "g"}
        ci.upsert("fathom", [
            dict(base, id="fathom:1", title="Investor call", scope="preference"),
            dict(base, id="fathom:2", title="Design Review", scope="team"),
        ])

        # The fake recovers the scope from SCOPE_Q itself rather than from the
        # question's wording. Keying on phrases inside the prompt made this
        # suite fail the moment the questions were reworded -- which is the
        # brittleness jev.py's docstring warns about, reproduced in a test.
        Q2K = {v: k for k, v in rc.SCOPE_Q.items()}

        def fake(question, texts):
            key = Q2K[question]
            return [0.9 if key in t else 0.1 for t in texts]

        got = rc.rescope(["this is organization", "this is team"], scorer=fake)
        check("argmax picks the highest-scoring scope", got == ["organization", "team"], got)

        # Batching must not drop or reorder. 95 records over BATCH=40 is 3 batches.
        rc.BATCH = 40
        many = ["this is team"] * 55 + ["this is organization"] * 40
        got = rc.rescope(many, scorer=fake)
        check("batching preserves count", len(got) == 95, len(got))
        check("batching preserves order",
              got[:55] == ["team"] * 55 and got[55:] == ["organization"] * 40)

        # A record nothing matches must come back unscoped, not be assigned the
        # scope whose rounding happened to win.
        flat = rc.rescope(["nothing matches"], scorer=fake)
        check("a record with no signal is unscoped, not guessed",
              flat == ["unscoped"], flat)

        def nearly_tied(question, texts):
            k = Q2K[question]
            return [0.42 if k == "team" else 0.40 for _ in texts]
        check("a win inside the gap is unscoped",
              rc.rescope(["x"], scorer=nearly_tied) == ["unscoped"])

        def clear(question, texts):
            k = Q2K[question]
            return [0.80 if k == "team" else 0.10 for _ in texts]
        check("a clear win is kept", rc.rescope(["x"], scorer=clear) == ["team"])

        # Dry run must not touch the store. This is the safety property.
        before = ci.path_for("fathom").read_text()
        rc.rescope_applied = None
        recs = ci.read("fathom")
        check("store is unchanged after a rescope call",
              ci.path_for("fathom").read_text() == before)
        check("scopes on disk are still the originals",
              [r["scope"] for r in recs] == ["preference", "team"],
              [r["scope"] for r in recs])

        # The scope the writer accepts is the scope this module emits — a drift
        # here would fail only at write time, after the expensive part ran.
        check("every scope this emits is one the writer accepts",
              set(rc.SCOPE_Q) <= set(ci.SCOPES), sorted(set(rc.SCOPE_Q) - set(ci.SCOPES)))

        # stdin parsing: both shapes, and blanks dropped rather than scored.
        sys.stdin = type("S", (), {"read": staticmethod(lambda: json.dumps(
            {"fathom:1": "  a  summary  ", "fathom:9": ""}))})()
        d = rc.read_summaries()
        check("stdin object form parses and normalises whitespace",
              d == {"fathom:1": "a summary"}, d)
        sys.stdin = type("S", (), {"read": staticmethod(lambda:
            '{"id":"fathom:2","summary":"x"}\n{"id":"fathom:3","gist":"y"}')})()
        check("stdin jsonl form parses, gist is a fallback for summary",
              rc.read_summaries() == {"fathom:2": "x", "fathom:3": "y"})

    print()
    if fails:
        print(f"FAIL — {len(fails)}/{ran[0]} checks red: {', '.join(fails)}")
        return 1
    print(f"PASS — {ran[0]}/{ran[0]} checks green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
