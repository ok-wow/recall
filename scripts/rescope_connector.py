#!/usr/bin/env python3
"""rescope_connector — re-scope indexed records from their summaries.

The half of the collector that has no network. The other half cannot live here:
a meeting summary is reachable only through MCP, which runs only inside an agent
session, so the agent fetches and this decides. Everything testable is therefore
on this side of the line, which is the point of the split.

Why it exists. 201 meetings were scoped from title plus invitee list, because
that is what one cheap listing call returns. Measured 2026-09-21 on a four-case
sample: two of two external one-to-ones were wrong and two of two internal
controls were right. A title is metadata a human wrote for a different purpose —
for a one-to-one it is just a person's name — so scoping on it measures the
naming convention. "Jeff Morris Jr. (Chapter One)" read as mentoring; the
summary's first line says a $3-5M fundraise.

    <ids+summaries on stdin> | rescope_connector.py fathom          # dry run
    <ids+summaries on stdin> | rescope_connector.py fathom --apply  # write

Dry run is the default deliberately. A re-scope rewrites records that are
already being served, and a pass that silently moved 200 of them would be
indistinguishable from a pass that moved the right three.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# One proposition per scope, ranked against each other per record, argmax taken.
# Not a per-scope threshold: the ordering is the calibrated part and the absolute
# numbers are not, which is the lesson the four-axis gate was built to relearn.
# Scope is about WHOSE KNOWLEDGE this is, not who was in the room. The first
# wording described attendees, and a dry run caught it: an internal call that
# settled pricing, usage caps and go-to-market scored `organization` — which is
# correct under the taxonomy and wrong under the question, because nobody
# external attended. A company decision is company knowledge whoever made it.
SCOPE_Q = {
    "organization": "knowledge belonging to the whole company and outliving any "
                    "team — pricing, positioning, fundraising, compliance, "
                    "customer and vendor relationships, legal commitments",
    "team": "knowledge about how this team builds — design decisions, "
            "engineering plans, architecture, roadmap sequencing, release scope",
    "preference": "how one particular person likes to work, or advice and "
                  "mentoring directed at an individual",
    "personal": "a private matter — someone's health, employment, family or "
                "social life — rather than the work",
}
# 201 candidates in one request returned HTTP 503. 40 goes through; the gateway's
# limit is payload size, not rate, so smaller batches beat longer sleeps.
BATCH = 40


def load_key(env_path: Path = None) -> None:
    """Read the gateway key from the one .env that holds it. Never echoed."""
    p = env_path or Path.home() / "dev/ok-wow-ai/.env"
    if os.environ.get("AI_GATEWAY_API_KEY") or not p.exists():
        return
    for line in p.read_text().splitlines():
        m = re.match(r"\s*AI_GATEWAY_API_KEY\s*=\s*(.+)", line)
        if m:
            os.environ["AI_GATEWAY_API_KEY"] = m.group(1).strip().strip("'\"")
            return


def rescope(texts: list[str], scorer=None) -> list[str]:
    """One scope per text. Batched, with backoff, because the gateway 503s."""
    if not texts:
        return []
    if scorer is None:
        import jev
        scorer = jev.score
    cols = {k: [] for k in SCOPE_Q}
    for s in range(0, len(texts), BATCH):
        chunk = texts[s:s + BATCH]
        for k, q in SCOPE_Q.items():
            for attempt in range(1, 6):
                try:
                    cols[k].extend(scorer(q, chunk))
                    break
                except Exception:
                    if attempt == 5:
                        raise
                    time.sleep(3 * attempt)
            time.sleep(0.8)
    return [max(SCOPE_Q, key=lambda k: cols[k][i]) for i in range(len(texts))]


def read_summaries() -> dict:
    raw = sys.stdin.read().strip()
    if not raw:
        raise SystemExit("rescope_connector: give {id: summary} JSON on stdin")
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        d = {}
        for line in raw.splitlines():
            if line.strip():
                r = json.loads(line)
                d[r["id"]] = r.get("summary") or r.get("gist") or ""
    if not isinstance(d, dict):
        raise SystemExit("rescope_connector: expected an object of id -> summary")
    return {k: " ".join(str(v).split()) for k, v in d.items() if str(v).strip()}


def main() -> int:
    import connector_index as ci
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.stderr.write("usage: rescope_connector.py <source> [--apply]\n")
        return 2
    source, apply = args[0], "--apply" in sys.argv
    load_key()
    summaries = read_summaries()
    existing = {r["id"]: r for r in ci.read(source)}
    missing = [i for i in summaries if i not in existing]
    if missing:
        sys.stderr.write(f"rescope_connector: {len(missing)} id(s) not in "
                         f"{source}: {', '.join(missing[:4])}\n")
    ids = [i for i in summaries if i in existing]
    if not ids:
        sys.stderr.write("rescope_connector: nothing to do\n")
        return 1
    scopes = rescope([summaries[i] for i in ids])

    import connector_index as _ci
    changed, held, out, regist, toolong = [], 0, [], 0, 0
    for i, new in zip(ids, scopes):
        old = existing[i].get("scope") or "unscoped"
        rec = dict(existing[i])
        moved = new != old
        if moved:
            changed.append((i, old, new, existing[i].get("title", "")))
            rec["scope"] = new
        else:
            held += 1
        # The fetch is the expensive half. Throwing the text away and keeping
        # only a scope wastes it -- the first version did exactly that and left
        # "Meeting, no listed invitees" on a record whose summary held the whole
        # pricing decision. Store the supplied text as the gist when it fits.
        text = summaries[i]
        if len(text) <= _ci.GIST_CAP and text != rec.get("gist"):
            rec["gist"] = text
            regist += 1
        elif len(text) > _ci.GIST_CAP:
            toolong += 1
        if moved or rec.get("gist") != existing[i].get("gist"):
            out.append(rec)

    print(f"  {len(ids)} re-scoped from summaries · {len(changed)} changed · {held} held")
    print(f"  {regist} gist(s) replaced from the supplied text"
          + (f" · {toolong} too long to store (cap {_ci.GIST_CAP}), scope only"
             if toolong else ""))
    for i, old, new, title in changed:
        print(f"    {old:<13} -> {new:<13} {title[:48]}")
    if not apply:
        print("\n  dry run — nothing written. Re-run with --apply.")
        return 0
    if out:
        print(" ", ci.upsert(source, out))
    else:
        print("  nothing to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
