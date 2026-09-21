#!/usr/bin/env python3
"""Would retrieval have found it? Ask the decisions you already made.

Every measurement of a retriever needs ground truth, and labelling it is the
reason most retrievers are never measured at all. Your transcripts already hold
some: every time a session loaded a SKILL, it decided which procedure that
moment called for. Replay the moment through the retriever and see where that
skill lands. Nobody labels anything.

    replay.py                 rank each past skill choice with the BM25 recall uses
    replay.py --judge         also rank it with the judgment model, and compare
    replay.py --json          machine-readable
    replay.py --misses        the moments retrieval missed, worst first

THE SPLIT THAT MAKES IT HONEST. Two kinds of invocation look identical in a
transcript and mean opposite things:

  NAMED    the person typed the skill's name, or /slug. Nothing was discovered.
           A retriever scores high here by reading the name back, and the number
           says nothing about finding anything.
  CHOSEN   the model picked it out of the description list, unprompted. This is
           the moment a proactive router would have to reproduce, and the only
           group worth reading.

Reported together they flatter the retriever, which is why they are never
reported together here.

WHAT THIS CANNOT TELL YOU. That a session chose a skill does not make it the
RIGHT skill — it is what happened, not what should have. So a miss can mean the
retriever failed OR that the original choice was poor, and this tool cannot tell
those apart. Read the rate as a comparison between retrievers, which it measures
honestly, and not as an absolute score of either one.

Read-only. It opens transcripts and skill files and writes nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HOST_DIR = Path(os.environ.get("RECALL_HOST_DIR") or Path.home() / ".claude").expanduser()
TRANSCRIPTS = Path(os.environ.get("RECALL_TRANSCRIPTS_DIR") or HOST_DIR / "projects")
SKILLS_DIR = Path(os.environ.get("RECALL_SKILLS_DIR") or HOST_DIR / "skills")
# A prompt long enough to hold the request and short enough that one stray
# paste does not become the whole query.
SITUATION_CAP = 1200

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _recall():
    import recall
    return recall


def load_skills(skills_dir: Path = None) -> dict[str, dict]:
    """name -> an entry shaped like a catalog entry, so the REAL bm25 ranks it.

    Indexed on name + description, because that pair is what an agent is shown
    when it picks a skill. Indexing the body instead was measured and is worse:
    it adds vocabulary the chooser never sees, and swamps the name.
    """
    out = {}
    for md in sorted((skills_dir or SKILLS_DIR).glob("*/SKILL.md")):
        name = md.parent.name
        if name.startswith("_"):
            continue
        try:
            raw = md.read_text(errors="replace")
        except OSError:
            continue
        desc = ""
        if raw.startswith("---"):
            end = raw.find("\n---", 3)
            if end > 0:
                m = re.search(r"^description:\s*(.+?)(?=^\w+:|\Z)", raw[3:end], re.S | re.M)
                desc = " ".join(m.group(1).split()) if m else ""
        out[name] = {"key": name, "id": name, "catalog": "skill", "recurrences": 0,
                     "raw": {}, "text": f"{name} {desc}", "probe_when": []}
    return out


def _typed_text(rec: dict) -> str:
    """Text the PERSON typed. A user turn carrying a tool result is not a prompt."""
    m = rec.get("message") or {}
    if rec.get("type") != "user" or m.get("role") != "user":
        return ""
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def moments(transcripts: Path = None) -> tuple[list[dict], dict]:
    """Every skill invocation that has a typed prompt in front of it."""
    root = transcripts or TRANSCRIPTS
    rows, stats = [], {"invocations": 0, "no_prompt": 0, "files": 0}
    for path in sorted(root.rglob("*.jsonl")):
        try:
            raw = path.read_text(errors="replace")
        except OSError:
            continue
        # A cheap prefilter so 3,000 transcripts are not all parsed. It matches
        # the bare token, NOT '"name":"Skill"' — that pair only appears in
        # compact JSON, so a file written with spaces after the colons was
        # skipped in silence and its invocations never counted.
        if '"Skill"' not in raw:
            continue
        last, hit = "", False
        for line in raw.splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            text = _typed_text(rec)
            # Hook output and injected reminders arrive on a user turn but are
            # not the person asking for anything. Scoring a router against text
            # it will never be given would measure the wrong thing.
            if text and "<system-reminder>" not in text[:200] and not text.startswith("Caveat:"):
                last = text
                continue
            content = (rec.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if not (isinstance(b, dict) and b.get("type") == "tool_use"
                        and b.get("name") == "Skill"):
                    continue
                stats["invocations"] += 1
                # Counted here, not at the prefilter: that guard passes any file
                # containing the word, and reporting those as "transcripts with a
                # skill call" overstated the corpus threefold.
                if not hit:
                    stats["files"] += 1
                    hit = True
                skill = (b.get("input") or {}).get("skill") or ""
                if not skill or not last:
                    stats["no_prompt"] += 1
                    continue
                slug = skill.split(":")[-1]          # plugin skills are plugin:name
                rows.append({
                    "situation": last[:SITUATION_CAP], "skill": skill, "slug": slug,
                    "named": slug.lower() in last.lower() or skill.lower() in last.lower(),
                    "file": path.name,
                })
    return rows, stats


def rank_all(rows: list[dict], skills: dict, field: str = "rank") -> None:
    """Position of the chosen skill in BM25's ranking, or None if unretrieved."""
    bm25 = _recall().bm25
    ordered = list(skills.values())
    for r in rows:
        ranked = bm25(ordered, r["situation"])
        r[field] = next((i for i, (e, _, _) in enumerate(ranked, 1)
                         if e["id"] == r["slug"]), None)


def score(rows: list[dict], field: str = "rank") -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    found = [r[field] for r in rows if r.get(field)]
    return {"n": n, "unretrieved": n - len(found),
            "hit@1": round(100 * sum(1 for p in found if p <= 1) / n, 1),
            "hit@3": round(100 * sum(1 for p in found if p <= 3) / n, 1),
            "hit@5": round(100 * sum(1 for p in found if p <= 5) / n, 1),
            "hit@10": round(100 * sum(1 for p in found if p <= 10) / n, 1),
            "mrr": round(sum(1 / p for p in found) / n, 3)}


def _print(label: str, s: dict, note: str = "") -> None:
    if not s.get("n"):
        return
    print(f"{label}   n={s['n']}{note}")
    print(f"  hit@1 {s['hit@1']:5.1f}%   hit@3 {s['hit@3']:5.1f}%   "
          f"hit@5 {s['hit@5']:5.1f}%   hit@10 {s['hit@10']:5.1f}%")
    print(f"  MRR   {s['mrr']:5.3f}     never retrieved at all: {s['unretrieved']}\n")


def main() -> int:
    ap = argparse.ArgumentParser(prog="replay", description=__doc__.splitlines()[0])
    ap.add_argument("--judge", action="store_true",
                    help="also rank with the judgment model (needs AI_GATEWAY_API_KEY)")
    ap.add_argument("--limit", type=int, default=0,
                    help="judge at most this many moments (0 = all)")
    ap.add_argument("--misses", action="store_true", help="list the moments retrieval missed")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    skills = load_skills()
    if not skills:
        sys.stderr.write(f"replay: no skills under {SKILLS_DIR}\n")
        return 2
    rows, stats = moments()
    known = [r for r in rows if r["slug"] in skills]
    # A skill that is not in the candidate set cannot be ranked, and counting it
    # as a miss would blame the retriever for a library it was never given.
    foreign = len(rows) - len(known)
    rank_all(known, skills)
    named = [r for r in known if r["named"]]
    chosen = [r for r in known if not r["named"]]

    if a.judge and chosen:
        import jev
        pool = chosen[:a.limit] if a.limit else chosen
        texts = [s["text"] for s in skills.values()]
        names = list(skills)
        done = 0
        for r in pool:
            try:
                scores = jev.score(r["situation"], texts)
            except jev.Unavailable as exc:
                sys.stderr.write(f"replay: judge unavailable ({exc}) after {done} moments\n")
                break
            order = [n for n, _ in sorted(zip(names, scores), key=lambda kv: -kv[1])]
            r["jrank"] = order.index(r["slug"]) + 1 if r["slug"] in order else None
            done += 1

    out = {"skills": len(skills), "files": stats["files"],
           "invocations": stats["invocations"], "no_prompt": stats["no_prompt"],
           "foreign_skills": foreign,
           "named": score(named), "chosen": score(chosen)}
    judged = [r for r in chosen if "jrank" in r]
    if judged:
        out["chosen_judged"] = score(judged, "jrank")
        out["chosen_bm25_same_moments"] = score(judged, "rank")

    if a.json:
        print(json.dumps(out, indent=2))
        return 0

    print(f"\n{len(skills)} skills · {stats['files']} transcripts holding a skill call · "
          f"{stats['invocations']} invocations")
    print(f"  {stats['no_prompt']} had no typed prompt before them · "
          f"{foreign} used a skill outside this library\n")
    _print("NAMED by the person  (a control — it proves nothing)", out["named"])
    _print("CHOSEN by the model  (the measurement)", out["chosen"])
    if judged:
        _print("CHOSEN, ranked by the judge", out["chosen_judged"],
               f"  — same {len(judged)} moments")
        _print("CHOSEN, ranked by search, same moments", out["chosen_bm25_same_moments"])

    if a.misses:
        miss = sorted((r for r in chosen if not r["rank"] or r["rank"] > 3),
                      key=lambda r: (r["rank"] or 10**6), reverse=True)[:20]
        print(f"{len(miss)} shown of the moments search put outside the top 3:\n")
        for r in miss:
            where = f"rank {r['rank']}" if r["rank"] else "never retrieved"
            print(f"  {r['slug']:34} {where}")
            print(f"    {' '.join(r['situation'].split())[:110]}\n")

    print("A skill that was chosen is not proof it was the right one. Read these as a "
          "comparison\nbetween retrievers, not as a score for either.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
