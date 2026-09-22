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

        # The fake keys on SCOPE_Q's KEYS, never on the question wording. Keying
        # on phrases inside the prompt made this suite fail the moment the
        # questions were reworded -- the brittleness jev.py's docstring warns
        # about, reproduced in a test.
        #
        # Its signature is the classify shape, (item, propositions) -> {scope:
        # score}, not the reranker's (question, texts) -> [score]. That is the
        # whole change: one item, scored alone, against every proposition at
        # once. A fake that still took a list of texts would be testing a call
        # shape the code no longer makes.
        rc.PACE = 0                      # no sleeping in tests

        def fake(item, props):
            return {k: (0.9 if k in item else 0.1) for k in props}

        got = rc.rescope(["this is organization", "this is team"], classifier=fake)
        check("argmax picks the highest-scoring scope", got == ["organization", "team"], got)

        # Nothing batches any more, but count and order still have to survive
        # the loop -- the property the old batching test was really protecting.
        many = ["this is team"] * 55 + ["this is organization"] * 40
        got = rc.rescope(many, classifier=fake)
        check("every record gets exactly one scope", len(got) == 95, len(got))
        check("order is preserved",
              got[:55] == ["team"] * 55 and got[55:] == ["organization"] * 40)

        # Each record must be scored on its own. If the loop ever reintroduced a
        # batch, this fake would see more than one item in a call.
        seen = []

        def solo(item, props):
            seen.append(item)
            return {k: (0.9 if k in item else 0.1) for k in props}
        rc.rescope(["this is team", "this is organization", "this is team"], classifier=solo)
        check("one call per record, never a batch", len(seen) == 3, len(seen))
        check("each call carries one record only",
              all(isinstance(x, str) for x in seen))

        # A record nothing matches must come back unscoped, not be assigned the
        # scope whose rounding happened to win.
        flat = rc.rescope(["nothing matches"], classifier=fake)
        check("a record with no signal is unscoped, not guessed",
              flat == ["unscoped"], flat)

        def nearly_tied(item, props):
            return {k: (0.42 if k == "team" else 0.40) for k in props}
        check("a win inside the gap is unscoped",
              rc.rescope(["x"], classifier=nearly_tied) == ["unscoped"])

        def clear(item, props):
            return {k: (0.80 if k == "team" else 0.10) for k in props}
        check("a clear win is kept", rc.rescope(["x"], classifier=clear) == ["team"])

        # A transient failure must be retried, not turned into a wrong label.
        calls = [0]

        def flaky(item, props):
            calls[0] += 1
            if calls[0] < 3:
                raise RuntimeError("gateway said 529")
            return {k: (0.80 if k == "team" else 0.10) for k in props}
        check("a transient classifier failure is retried",
              rc.rescope(["x"], classifier=flaky) == ["team"], calls[0])

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
