#!/usr/bin/env python3
"""Fixtures for replay.py — measuring a retriever against past skill choices.

The load-bearing cases are the ones that decide whether a number is honest:
what counts as the prompt, what counts as a discovery rather than a person
reading a name back, and what is excluded rather than scored as a miss. A
retriever benchmark that gets those wrong reports a flattering number, which is
worse than reporting none.

Synthetic transcripts in a temp dir. No network, no real history.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(os.environ.get("RECALL_SKILL_DIR")
                  or Path(__file__).resolve().parent.parent / "scripts")
sys.path.insert(0, str(SCRIPT_DIR))
import replay  # noqa: E402

SKILLS = {
    "okwow-brainstorm": "Use when opening a new feature or an unscoped question, "
                        "to explore directions before committing to one.",
    "flaky-test-triage": "Use when a test passes alone and fails in a full run, "
                         "to find the shared state two tests are fighting over.",
    "deck-house-style": "Use when building slides, to apply the house typography "
                        "and layout canon.",
}


def skills_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="replayskills-"))
    for name, desc in SKILLS.items():
        p = d / name
        p.mkdir()
        (p / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\nBody.\n")
    # Underscore dirs are machinery, not skills, and must never be candidates.
    (d / "_archived").mkdir()
    (d / "_archived" / "SKILL.md").write_text("---\nname: old\ndescription: gone\n---\n")
    return d


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def user_blocks(*blocks):
    return {"type": "user", "message": {"role": "user", "content": list(blocks)}}


def skill_call(name):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Skill", "input": {"skill": name}}]}}


def transcripts(*records) -> Path:
    d = Path(tempfile.mkdtemp(prefix="replaytx-"))
    (d / "a.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return d


def main() -> int:
    fails, ran = [], [0]

    def check(name, cond, detail=""):
        ran[0] += 1
        print(f"  [{'ok ' if cond else 'FAIL'}] {name}" + ("" if cond else f"  {detail}"))
        if not cond:
            fails.append(name)

    sd = skills_dir()
    sk = replay.load_skills(sd)
    check("underscore dirs are not candidates", set(sk) == set(SKILLS), sorted(sk))
    check("a skill is indexed on name and description",
          "unscoped" in sk["okwow-brainstorm"]["text"] and
          "okwow-brainstorm" in sk["okwow-brainstorm"]["text"])

    # THE split. Same invocation, two prompts: one names it, one does not.
    tx = transcripts(
        user("run okwow-brainstorm on the new importer"), skill_call("okwow-brainstorm"),
        user("this test passes alone and fails in the full run"), skill_call("flaky-test-triage"),
    )
    rows, stats = replay.moments(tx)
    check("both invocations found", stats["invocations"] == 2, str(stats))
    by = {r["slug"]: r for r in rows}
    check("naming the skill is NAMED", by["okwow-brainstorm"]["named"])
    check("describing the problem is CHOSEN", not by["flaky-test-triage"]["named"])

    # A tool RESULT arrives on a user turn. It is not the person asking.
    tx = transcripts(
        user("this test passes alone and fails in the full run"),
        user_blocks({"type": "tool_result", "content": "okwow-brainstorm okwow-brainstorm"}),
        skill_call("flaky-test-triage"))
    rows, _ = replay.moments(tx)
    check("a tool result is not the prompt",
          rows and rows[0]["situation"].startswith("this test passes"),
          rows[0]["situation"][:50] if rows else "no rows")

    # Injected reminders arrive the same way and must not become the query.
    tx = transcripts(
        user("this test passes alone and fails in the full run"),
        user("<system-reminder>\nremember to use deck-house-style\n</system-reminder>"),
        skill_call("flaky-test-triage"))
    rows, _ = replay.moments(tx)
    check("a system reminder is not the prompt",
          rows and rows[0]["situation"].startswith("this test passes"),
          rows[0]["situation"][:50] if rows else "no rows")

    # An invocation with nothing before it is counted, never scored, never fatal.
    tx = transcripts(skill_call("flaky-test-triage"))
    rows, stats = replay.moments(tx)
    check("an invocation with no prompt is counted, not scored",
          rows == [] and stats["no_prompt"] == 1, str(stats))

    # A plugin skill outside the library is not a retrieval miss.
    tx = transcripts(user("do the thing"), skill_call("some-plugin:not-installed"))
    rows, _ = replay.moments(tx)
    known = [r for r in rows if r["slug"] in sk]
    check("a foreign skill is excluded, not counted as a miss",
          len(rows) == 1 and known == [])

    # Ranking: a described problem should retrieve its skill.
    tx = transcripts(
        user("a test passes alone and fails in the full run, shared state somewhere"),
        skill_call("flaky-test-triage"))
    rows, _ = replay.moments(tx)
    replay.rank_all(rows, sk)
    check("a described problem retrieves its skill", rows[0]["rank"] == 1,
          f"rank={rows[0]['rank']}")

    # A prompt sharing no vocabulary retrieves nothing, and that is recorded as
    # unretrieved rather than silently dropped from the denominator.
    tx = transcripts(user("zzzz qqqq"), skill_call("deck-house-style"))
    rows, _ = replay.moments(tx)
    replay.rank_all(rows, sk)
    s = replay.score(rows)
    check("an unretrieved moment stays in the denominator",
          s["n"] == 1 and s["unretrieved"] == 1 and s["hit@10"] == 0.0, str(s))

    # The arithmetic itself.
    fake = [{"rank": 1}, {"rank": 2}, {"rank": 4}, {"rank": None}]
    s = replay.score(fake)
    check("hit@k counts ranks at or below k", (s["hit@1"], s["hit@3"], s["hit@5"]) == (25.0, 50.0, 75.0), str(s))
    check("MRR divides by every moment, not just the found ones",
          s["mrr"] == round((1 + 0.5 + 0.25) / 4, 3), str(s))
    check("an empty set does not divide by zero", replay.score([])["n"] == 0)

    print()
    if fails:
        print(f"FAIL — {len(fails)}/{ran[0]} checks red: {', '.join(fails)}")
        return 1
    print(f"PASS — {ran[0]}/{ran[0]} checks green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
