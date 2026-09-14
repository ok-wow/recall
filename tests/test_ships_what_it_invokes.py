#!/usr/bin/env python3
"""Every external name the shipped code invokes must resolve inside this repo.

This exists because the first extraction of this project shipped the capture
hook, the queue, the drain, the index builder and recall -- and not the skill
the drain invokes. A fresh clone captured sessions, drained them, ran
`<agent> -p "/compound <id>"` against a command the cloner did not have, wrote
nothing, and kept an empty corpus forever.

Five suites were green over it. The drain suite substitutes the agent with
`#!/bin/sh exit 0` to test marker lifecycle, so the stub stood exactly where
the missing component belonged. A stub is an admission that the suite cannot
see past a boundary; this file checks the far side of that boundary statically.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        fails.append(f"{label}{(' — ' + detail) if detail else ''}")


def shipped_sources() -> list[Path]:
    out = []
    for d in ("hooks", "scripts", "skills"):
        out += [p for p in (ROOT / d).rglob("*") if p.is_file()]
    out.append(ROOT / "install.sh")
    return out


# -- the two scanners, written as pure functions so they can be negative-controlled --

SLASH_CMD = re.compile(r'-p\s+"(?:[^"]*?)/([a-z][a-z0-9-]*)[\s"]')
SIBLING = re.compile(r'\$(SCRIPT_DIR|SKILL_DIR|RECALL_SKILL_DIR|REPO_DIR)/([A-Za-z0-9_./-]+)')

# The VARIABLE decides the base directory, not the file doing the referring:
# $SKILL_DIR resolves to scripts/ even when a hook in hooks/ names it.
VAR_BASE = {
    "SKILL_DIR": ROOT / "scripts",
    "RECALL_SKILL_DIR": ROOT / "scripts",
    "REPO_DIR": ROOT,
}


def missing_skills(text: str, has_skill) -> set[str]:
    return {c for c in SLASH_CMD.findall(text) if not has_skill(c)}


def missing_siblings(text: str, script_dir: Path) -> set[str]:
    out = set()
    for var, rel in SIBLING.findall(text):
        if "$" in rel or rel.endswith("/"):
            continue
        base = VAR_BASE.get(var, script_dir)
        if not (base / rel).exists():
            out.add(f"${var}/{rel}")
    return out


# ---------------------------------------------------------------- real checks --

def skill_exists(name: str) -> bool:
    return (ROOT / "skills" / name / "SKILL.md").is_file()


for src in shipped_sources():
    try:
        text = src.read_text()
    except UnicodeDecodeError:
        continue
    rel = src.relative_to(ROOT)
    for name in missing_skills(text, skill_exists):
        check(f"{rel} invokes /{name} but skills/{name}/SKILL.md is not shipped", False)
    for ref in missing_siblings(text, src.parent):
        check(f"{rel} references {ref} which this repo does not ship", False)

# The drain's own invocation is the one that mattered; assert it explicitly
# rather than relying on the scan to have found the file.
drain = (ROOT / "hooks" / "recall-drain.sh").read_text()
check("drain still invokes a slash command", '-p "/compound' in drain)
check("the skill it invokes is shipped", skill_exists("compound"))
_skill = ROOT / "skills" / "compound" / "SKILL.md"
# Read defensively: when this check is the one failing, the file is missing, and
# a traceback names the path far less clearly than the check above does.
check("shipped skill has frontmatter name",
      _skill.is_file() and "name: compound" in _skill.read_text())
check("install.sh installs the skill",
      "skills/compound" in (ROOT / "install.sh").read_text())
check("drain exports the skill dir the SKILL.md tells the agent to use",
      "RECALL_SKILL_DIR=" in drain)

# An env var a test SETS but no shipped code READS is isolation theatre: the
# suite believes it redirected something and is quietly using the real thing.
# test_recall set RECALL_PROBE_INDEX for months while all three readers
# hardcoded RECALL_HOME/probe-index.json.
# Key position only: `"RECALL_X": v` is a var handed to the SUT's environment.
# `os.environ.get("RECALL_X")` is the test configuring ITSELF, which is fine.
ENV_RE = re.compile(r'"(RECALL_[A-Z_]+)"\s*:')
set_by_tests: set[str] = set()
_self = Path(__file__).name
for t in (ROOT / "tests").glob("test_*.py"):
    if t.name == _self:
        continue          # this file's own comments and controls are not fixtures
    set_by_tests |= set(ENV_RE.findall(t.read_text()))
read_by_code = set()
for src in shipped_sources():
    try:
        read_by_code |= set(re.findall(r"RECALL_[A-Z_]+", src.read_text()))
    except UnicodeDecodeError:
        pass
for var in sorted(set_by_tests - read_by_code):
    check(f"tests set {var} but no shipped code reads it — that isolation is imaginary", False)

# -- negative controls: a check that cannot fail proves nothing --------------
check("scanner detects a missing skill",
      missing_skills('-p "/nosuchskill abc"', skill_exists) == {"nosuchskill"})
check("scanner detects a missing sibling",
      missing_siblings('"$SCRIPT_DIR/not-shipped.py"', ROOT / "hooks") == {"$SCRIPT_DIR/not-shipped.py"})
check("scanner resolves $SKILL_DIR against scripts/, not the referring dir",
      missing_siblings('"$SKILL_DIR/recall.py"', ROOT / "hooks") == set())
check("scanner does not flag a skill that IS shipped",
      missing_skills('-p "/compound abc"', skill_exists) == set())

total = 6 + 4
if fails:
    print(f"FAIL {len(fails)} check(s):")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASS {total}/{total} invocation-resolution checks (4 controls)")
