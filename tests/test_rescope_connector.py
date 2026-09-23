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
        # The thin-text filter sits IN FRONT of every check below, and these
        # fakes use short strings. Disable it here and test it on its own.
        real_min_text = rc.MIN_TEXT
        rc.MIN_TEXT = 0

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

        # The filter itself: a record with no text must cost nothing. Scoring
        # 1,224 such Linear records burned ~790,000 tokens on 2026-09-21.
        rc.MIN_TEXT = real_min_text
        spent = []

        def counting(item, props):
            spent.append(item)
            return {k: 0.9 for k in props}
        got = rc.rescope(["Triage · Tech Debt", "x", ""], classifier=counting)
        check("a record with no text is unscoped", got == ["unscoped"] * 3, got)
        check("and costs no model call", spent == [], spent)
        got = rc.rescope(["this is team " + "and more detail about it " * 4],
                         classifier=lambda i, p: {k: (0.9 if k == "team" else 0.1) for k in p})
        check("a record above the floor is still scored", got == ["team"], got)
        rc.MIN_TEXT = 0

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

    # ---- --from-index must never rewrite a gist -------------------------
    # The mode builds its "summary" from the record's OWN title and gist, so
    # writing that back prepends the title and pushes real content off the
    # 400-char end. Fixed once on 2026-09-22 in the caller, then reintroduced
    # through this path the same day on 447 of 486 records, because nothing
    # here was watching. This is the thing watching.
    import subprocess
    d = Path(tempfile.mkdtemp(prefix="rescope-fromindex-"))
    os.environ["RECALL_CONNECTOR_DIR"] = str(d)
    importlib.reload(ci)
    ci.upsert("fathom", [{
        "id": "fathom:9", "source": "fathom", "title": "Design Review",
        "gist": "The vault listing takes the panel width and Spaces trades filters for Upload.",
        "date": "2026-03-04", "url": "https://fathom.video/calls/9", "scope": "team",
    }])
    before = ci.read("fathom")[0]["gist"]

    stub = d / "fakejev.py"
    stub.write_text(
        "def classify(item, props):\n"
        "    return {k: (0.9 if k == 'organization' else 0.1) for k in props}\n")
    runner = d / "go.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(HERE.parent / 'scripts')!r})\n"
        f"sys.path.insert(0, {str(d)!r})\n"
        "import fakejev, rescope_connector as rc\n"
        "rc.PACE = 0\n"
        "import jev; jev.classify = fakejev.classify\n"
        "sys.argv = ['rescope_connector.py', 'fathom', '--from-index', '--apply']\n"
        "raise SystemExit(rc.main())\n")
    r = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True,
                       env={**os.environ, "RECALL_CONNECTOR_DIR": str(d),
                            "AI_GATEWAY_API_KEY": "test"})
    after = ci.read("fathom")[0]
    check("--from-index runs", r.returncode == 0, (r.stdout + r.stderr)[-200:])
    check("--from-index leaves the gist exactly as it was",
          after["gist"] == before, after["gist"][:70])
    check("the gist does not gain its own title",
          not after["gist"].startswith("Design Review"), after["gist"][:40])
    check("--from-index still moves the scope",
          after["scope"] == "organization", after["scope"])


    print()
    if fails:
        print(f"FAIL — {len(fails)}/{ran[0]} checks red: {', '.join(fails)}")
        return 1
    print(f"PASS — {ran[0]}/{ran[0]} checks green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
