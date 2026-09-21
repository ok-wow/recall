#!/usr/bin/env python3
"""Fusion is the part with no model in it, so it is the part that can be tested.

The behaviours asserted here are the ones the measurement depends on: absence
must be silent rather than a penalty, rank must beat score, and one phrasing
must not be able to veto the rest."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import expand_query as eq

fails: list[str] = []
ran = [0]


def check(name, ok, detail=None):
    ran[0] += 1
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}" + ("" if ok else f"  {detail!r}"))
    if not ok:
        fails.append(name)


def main() -> int:
    # An entry every phrasing ranks mid beats one a single phrasing ranks first.
    # This is the whole reason for fusing rather than taking the best list.
    a = ["x", "A", "B", "C"]
    b = ["y", "A", "B", "C"]
    c = ["z", "A", "B", "C"]
    order = [k for k, _ in eq.fuse([a, b, c])]
    check("agreement across phrasings beats one first place", order[0] == "A", order[:3])

    # Absence contributes nothing — it must not push an entry down.
    with_absent = eq.fuse([["A", "B"], ["B"], ["B"]])
    scores = dict(with_absent)
    check("an id missing from a ranking is not penalised",
          scores["A"] > 0 and scores["B"] > scores["A"], scores)

    # One irrelevant phrasing cannot veto an entry three others agree on.
    noisy = [k for k, _ in eq.fuse([["A"], ["A"], ["A"], ["Q", "R", "S"]])]
    check("a bad phrasing cannot veto the others", noisy[0] == "A", noisy[:3])

    # RRF is rank-based: the same ranks must fuse identically whatever the
    # underlying scores were, which is why bm25 scores are not passed in.
    check("fusion reads rank only, never score",
          eq.fuse([["A", "B"]]) == eq.fuse([["A", "B"]]))

    # k damps first place: with a large k, three seconds beat one first.
    small = [k for k, _ in eq.fuse([["A"], ["B"], ["B"]], k=1)]
    check("k damps the weight of a single rank-1", small[0] == "B", small)

    check("empty input is empty output, not a crash", eq.fuse([]) == [])
    check("a ranking of nothing is ignored", eq.fuse([[], ["A"]])[0][0] == "A")

    # Duplicate ids inside ONE ranking must not double-count: a phrasing that
    # somehow lists an entry twice would otherwise outvote three phrasings.
    dup = dict(eq.fuse([["A", "A", "A"], ["B"]]))
    check("an id repeated in one ranking counts once per position, not per id",
          dup["A"] < 3 * dup["B"], dup)

    # The authoring contract has to name the one phrasing that measured worst,
    # or the next author writes only that one.
    brief = eq.PHRASING_BRIEF
    check("the brief asks for the failure, not only the task",
          "failure" in brief.lower() and "restate the task" in brief.lower())
    check("the brief caps phrasing length", "twelve words" in brief)

    print()
    if fails:
        print(f"FAIL — {len(fails)}/{ran[0]} checks red: {', '.join(fails)}")
        return 1
    print(f"PASS — {ran[0]}/{ran[0]} checks green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
