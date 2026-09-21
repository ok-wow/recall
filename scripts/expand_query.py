#!/usr/bin/env python3
"""expand_query — retrieve under several phrasings of one intent, then fuse.

Measured 2026-09-21. An agent about to do something asks in TASK voice ("I am
adding a data source to a tool that has tests"). The catalogue is written in
FAILURE voice ("a test harness that drives the real component corrupts the
metric it produces"). Across five phrasings of a single intent the preventing
entry landed anywhere from NOT RETRIEVED to top-5, and the two that worked used
the corpus's own words. Stemming does not close it: query and entry shared zero
tokens before stemming and zero after. The vocabularies are disjoint, so no
tokenizer change reaches across.

A reranker cannot help either, and that is the point this module exists to
answer. For the failing case BM25 returned 373 entries and the right one was
not among them at any depth -- reranking reorders a pool, it does not enlarge
one. The fixable half is the QUERY.

So: several phrasings, one pool, fused by reciprocal rank. RRF and not score
addition because BM25 scores are corpus-relative and not comparable between
queries of different lengths -- the same entry scores 34 under four words and 8
under twenty. Rank is comparable; the score is not.

Generation of the phrasings is NOT here, for the same reason the connector
collector is not in connector_index.py: it needs a model, and everything in
this file has to be testable without one. The calling agent writes the
phrasings and passes them in. PHRASING_BRIEF below is the contract it follows.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The contract the calling agent fills. Each line earns its place from the
# measurement: naming the failure was the only phrasing that reliably worked,
# and naming the task -- the one an agent produces unprompted -- was the only
# one that reliably did not.
PHRASING_BRIEF = """\
You are about to do a piece of work. Write 4-6 short search phrasings of the
SAME intent, for a catalogue written as past failures and their fixes.

  1. Name the failure this work could become, in plain words.
  2. Name it again as a consequence someone would complain about.
  3. Name the mechanism or artifact you are touching, with its real noun.
  4. Name what you would have to check to know you got it right.
  5. Restate the task itself.

Rules: no shared boilerplate between lines; do not reuse the task's own
wording in lines 1-4; prefer the vocabulary of things going wrong. Keep each
line under twelve words."""

RRF_K = 60          # standard; large enough that rank 1 does not dominate a fused list


def fuse(rankings: list[list[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Reciprocal rank fusion over several ranked id lists.

    An id absent from a ranking contributes nothing rather than a penalty: a
    phrasing that never retrieves an entry is silent about it, not evidence
    against it. Penalising absence would let one bad phrasing veto a good one.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for i, key in enumerate(ranking):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + i + 1)
    return sorted(scores.items(), key=lambda kv: -kv[1])


def expand(phrasings: list[str], entries=None, per_query: int = 25) -> list[tuple[str, float]]:
    """Run each phrasing, fuse the ranked lists, return fused (key, score)."""
    import recall
    entries = entries if entries is not None else recall.load_entries()
    rankings = []
    for q in phrasings:
        if not str(q).strip():
            continue
        ranked = recall.bm25(entries, q)[:per_query]
        rankings.append([e["key"] for e, _, _ in ranked])
    return fuse(rankings)


def main() -> int:
    if "--brief" in sys.argv:
        print(PHRASING_BRIEF)
        return 0
    raw = sys.stdin.read().strip()
    if not raw:
        sys.stderr.write("expand_query: give one phrasing per line on stdin "
                         "(or --brief for the authoring contract)\n")
        return 2
    try:
        phrasings = json.loads(raw)
        if not isinstance(phrasings, list):
            phrasings = [str(phrasings)]
    except json.JSONDecodeError:
        phrasings = [l.strip() for l in raw.splitlines() if l.strip()]
    import recall
    entries = recall.load_entries()
    by_key = {e["key"]: e for e in entries}
    limit = 5
    if "-n" in sys.argv:
        limit = int(sys.argv[sys.argv.index("-n") + 1])
    fused = expand(phrasings, entries)[:limit]
    print(f"  {len(phrasings)} phrasings fused over {len(entries)} entries\n")
    for key, s in fused:
        e = by_key.get(key)
        if e:
            recall.show(e, score=s * 1000, hits=["fused"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
