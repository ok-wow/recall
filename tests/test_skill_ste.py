#!/usr/bin/env python3
"""Pin the hybrid prose standard on any skill that declares it.

A skill is served to a model at runtime, so its instructions are machine-facing
and get Strict ASD-STE100: one instruction per sentence, 20 words or fewer,
active voice, no phrasal verbs, no semicolons, no hedging.

But an instruction without its reason gets followed worse, not better, and this
project's whole argument is that a guard whose reason is forgotten gets removed.
So the reasons stay -- in blockquotes, in normal prose, binding nothing.

The split has to be structural or it rots: "remember to write the rules tersely"
is exactly the kind of prose rule this corpus records as never holding. Here the
rule is mechanical. OUTSIDE a blockquote is binding and checked. INSIDE one is
prose and ignored.

Opt in with `prose_standard: hybrid-ste-1` in the skill's frontmatter. A skill
that does not declare it is not checked.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STANDARD = "hybrid-ste-1"
MAX_WORDS = 20

# STE bans phrasal verbs: the particle changes the meaning and a second-language
# reader (or a tokenizer) cannot recover it. Each maps to a single-word verb.
PHRASAL = {
    "look up": "find", "carry out": "do", "set up": "prepare",
    "turn on": "start", "turn off": "stop", "find out": "learn",
    "come up with": "propose", "go through": "examine", "put in": "insert",
    "take out": "remove", "bring about": "cause", "make sure": "verify",
    "check out": "inspect", "figure out": "determine",
}
# STE wants definite instructions. A hedged rule is not a rule.
HEDGES = {"should", "could", "might", "may", "perhaps", "possibly", "probably"}

fails: list[str] = []


def rule_lines(text: str) -> list[tuple[int, str]]:
    """Every line that is BINDING: prose outside blockquotes, fences and tables."""
    out, in_fence, in_html = [], False, False
    lines = text.splitlines()
    # skip frontmatter
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break
    for n, raw in enumerate(lines[start:], start=start + 1):
        s = raw.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if "<!--" in s:
            in_html = True
        if in_html:
            if "-->" in s:
                in_html = False
            continue
        if not s or s.startswith(">") or s.startswith("#") or s.startswith("|"):
            continue          # blockquote = the human half; heading/table = labels
        if s.startswith("---"):
            continue
        out.append((n, s))
    return out


def sentences(line: str) -> list[str]:
    line = re.sub(r"`[^`]*`", "X", line)        # a code span is one token
    line = re.sub(r"^[-*]\s+", "", line)        # list marker is not a word
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+", line) if p.strip()]


def check_file(path: Path) -> None:
    text = path.read_text()
    if f"prose_standard: {STANDARD}" not in text:
        return
    rel = path.relative_to(ROOT)
    for n, line in rule_lines(text):
        low = line.lower()
        if ";" in line:
            fails.append(f"{rel}:{n} semicolon in a rule line — split the sentence")
        for p, better in PHRASAL.items():
            if re.search(rf"\b{p}\b", low):
                fails.append(f"{rel}:{n} phrasal verb '{p}' — use '{better}'")
        for s in sentences(line):
            words = s.split()
            if len(words) > MAX_WORDS:
                fails.append(f"{rel}:{n} {len(words)} words (max {MAX_WORDS}): {s[:58]}…")
            hedged = HEDGES & {w.strip(".,:!?").lower() for w in words}
            if hedged:
                fails.append(f"{rel}:{n} hedging {sorted(hedged)} — state the rule definitely")


checked = [p for p in (ROOT / "skills").rglob("SKILL.md")
           if f"prose_standard: {STANDARD}" in p.read_text()]
for p in checked:
    check_file(p)

# -- negative controls: a checker that cannot fail proves nothing --------------
CONTROLS = [
    ("a rule line over the cap",
     "Run the command and then read the output and then write the entry and then "
     "commit the result and finally report what you did.", True),
    ("a semicolon in a rule", "Run the command; read the output.", True),
    ("a phrasal verb", "Look up the entry before you write.", True),
    ("a hedge", "You should run the command first.", True),
    ("a clean rule", "Run the command before you write.", False),
]
for label, text, want_fail in CONTROLS:
    before = len(fails)
    probe = ROOT / "skills" / "_control" / "SKILL.md"
    saved = fails[:]
    fails.clear()
    for n, line in rule_lines(f"---\nprose_standard: {STANDARD}\n---\n{text}\n"):
        low = line.lower()
        if ";" in line:
            fails.append("semicolon")
        for ph in PHRASAL:
            if re.search(rf"\b{ph}\b", low):
                fails.append("phrasal")
        for s in sentences(line):
            w = s.split()
            if len(w) > MAX_WORDS:
                fails.append("length")
            if HEDGES & {x.strip(".,:!?").lower() for x in w}:
                fails.append("hedge")
    got_fail = bool(fails)
    fails.clear()
    fails.extend(saved)
    if got_fail != want_fail:
        fails.append(f"CONTROL BROKEN: '{label}' expected fail={want_fail}, got {got_fail}")

if not checked:
    print("no skill declares prose_standard — nothing to check")
    raise SystemExit(0)

if fails:
    print(f"FAIL {len(fails)} STE violation(s) across {len(checked)} skill(s):")
    for f in fails[:30]:
        print(f"  - {f}")
    if len(fails) > 30:
        print(f"  … and {len(fails) - 30} more")
    sys.exit(1)
print(f"PASS {len(checked)} skill(s) conform to {STANDARD} (5 controls)")
