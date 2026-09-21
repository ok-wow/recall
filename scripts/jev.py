#!/usr/bin/env python3
"""Ask a judgment model to rank the lessons BM25 already found.

Pull's problem is not absence, it is ordering. Measured over one session's 26
real queries (2026-09-21): the rank-1 result scored 0.56 mean relevance, while
65% of those queries held a >=0.70 lesson somewhere in the top 3. The right
answer is usually present and sitting below the wrong one. That is what a
ranker fixes and a better tokenizer does not.

Three things this module will not do, each for a measured reason:

1. **It never opens a second connection.** One call carries every candidate.
   The first evaluation ran four calls concurrently and 127 of 184 came back
   HTTP 429; a five-call serial probe went 5/5. The rate wall is client
   concurrency, so there is exactly one request here and no pool.
2. **It never applies a threshold.** Jev's ordering is the trustworthy part;
   its absolute numbers are not calibrated, and the same candidates under a
   thinner context string have been measured moving a score from 0.96 to 0.72.
   So this reorders and returns the scores. Deciding that 0.23 means "drop it"
   needs a calibration set, and eleven hand labels is not one.
3. **It never fails a query.** Every failure raises `Unavailable`, and the
   caller keeps BM25's order. A retrieval tool that returns nothing because a
   network call timed out is worse than one that returns a rougher ranking.

Standard library only. The whole project installs PyYAML and nothing else, and
a judge that adds a dependency is a judge that does not get installed.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Sequence

BASE_URL = os.environ.get("RECALL_JEV_URL",
                          "https://ai-gateway.vercel.sh/typesafe/v1/systemone")
MODEL = os.environ.get("RECALL_JEV_MODEL", "typesafe-ai/jev")
TIMEOUT = float(os.environ.get("RECALL_JEV_TIMEOUT", "20"))
ATTEMPTS = 3

# Held byte-identical to the surface the 2026-09-21 measurement used. The
# numbers quoted above only describe THIS wording; changing it invalidates them,
# so change it and re-measure together or not at all.
CONTEXT = (
    "An engineering agent stopped and searched a catalogue of past failures in "
    "plain language, looking for a lesson that helps with what it is doing. "
    "Each result is a past mistake plus its fix."
)
QUESTION = (
    "Lesson {name} answers what the engineer was asking: reading it would give "
    "them something they can act on for this question. A lesson that merely "
    "shares a word or a subject with the question does not qualify."
)


class Unavailable(Exception):
    """Jev could not answer. Always recoverable — the caller keeps BM25's order."""


def _post(body: dict, key: str) -> dict:
    req = urllib.request.Request(
        BASE_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def score(question: str, lessons: Sequence[str]) -> list[float]:
    """One relevance score per lesson, in the order given.

    Raises Unavailable for every failure, including a missing key — the caller
    treats "no judge" and "judge said nothing useful" the same way, because the
    recovery is the same.
    """
    key = os.environ.get("AI_GATEWAY_API_KEY", "").strip()
    if not key:
        raise Unavailable("AI_GATEWAY_API_KEY is not set")
    if not lessons:
        return []

    names = [f"lesson_{i}" for i in range(len(lessons))]
    body = {
        "model": MODEL,
        "state": {
            "context": CONTEXT,
            "question": question,
            "lessons": [{"id": n, "text": t} for n, t in zip(names, lessons)],
        },
        "questions": {n: {"type": "noul", "instructions": QUESTION.format(name=n)}
                      for n in names},
    }

    payload = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            payload = _post(body, key)
            break
        except urllib.error.HTTPError as exc:
            reason = f"HTTP {exc.code}"
            # 429 here means the gateway is busy, not that the request is wrong.
            retryable = exc.code in (429, 500, 502, 503, 504)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = exc.__class__.__name__
            retryable = True
        if not retryable or attempt == ATTEMPTS:
            raise Unavailable(reason)
        time.sleep(1.2 * attempt)

    answers = (payload or {}).get("answers")
    if not isinstance(answers, dict):
        raise Unavailable("response carried no answers")

    out = []
    for n in names:
        a = answers.get(n)
        if not isinstance(a, dict) or not isinstance(a.get("noul"), (int, float)):
            # Reading a missing answer as 0.0 would be a confident "irrelevant",
            # and a confident wrong answer is worse here than no answer at all.
            raise Unavailable(f"no usable answer for {n}")
        out.append(float(a["noul"]))
    return out
