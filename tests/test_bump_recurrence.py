#!/usr/bin/env python3
"""Recording that a lesson failed again — scripts/bump_recurrence.py.

The fixtures mirror the live catalogs' real shapes rather than a plausible
approximation: both id spellings (`- id:` when the id is the item's first key,
`  id:` when it is not), a note held as a string, an entry with no counter at
all, and a neighbour on each side to catch a write landing next door. Every
shape here cost a silent wrong write in this corpus at least once.

The assertions that matter are the refusals. A surgical YAML edit that hits the
wrong entry reads exactly like a correct one until something re-parses the file.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

SCRIPT_DIR = Path(os.environ.get("RECALL_SKILL_DIR")
                  or Path(__file__).resolve().parent.parent / "scripts")
BUMP = SCRIPT_DIR / "bump_recurrence.py"
TODAY = "2026_09_21"

# Written as TEXT, not yaml.safe_dump, because the shapes under test are
# textual: safe_dump emits one spelling and the corpus contains two.
CATALOG = """\
- id: first-entry-is-the-item-marker
  what: The first entry writes its id as the item's own first key.
  fix_pattern: Do the thing.
  probe_when:
  - '`alpha`'
  recurrences: 2
  recurrence_2026_09_01: An earlier repeat, already dated.
- what: This entry keeps its id further down, which is the common spelling.
  id: id-is-not-the-first-key
  fix_pattern: Do the other thing.
  recurrences: 0
- id: entry-with-no-counter-at-all
  what: Never recurred, so it carries no recurrences key.
  fix_pattern: "A fix with a colon: and an apostrophe's worth of trouble."
- id: the-neighbour-that-must-not-move
  what: Nothing should ever touch this one.
  fix_pattern: Untouched.
  recurrences: 7
"""


def run(path: Path, eid: str, note: str, today: str = TODAY) -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(BUMP), str(path), eid, "-"],
                       input=note, capture_output=True, text=True,
                       env={**os.environ, "RECALL_TODAY": today.replace("_", "-")})
    return p.returncode, p.stdout + p.stderr


def fresh() -> Path:
    d = Path(tempfile.mkdtemp(prefix="recallbump-"))
    f = d / "FAILURE_MODES.yaml"
    f.write_text(CATALOG)
    return f


def load(f: Path) -> dict:
    return {e["id"]: e for e in yaml.safe_load(f.read_text())}


def main() -> int:
    fails, ran = [], [0]

    def check(name, cond, detail=""):
        ran[0] += 1
        print(f"  [{'ok ' if cond else 'FAIL'}] {name}" + ("" if cond else f"  {detail}"))
        if not cond:
            fails.append(name)

    # 1. The counter and the date move together, on the `- id:` spelling.
    f = fresh()
    rc, out = run(f, "first-entry-is-the-item-marker", "The pull retried and hung again.")
    e = load(f)["first-entry-is-the-item-marker"]
    check("counter advances", rc == 0 and e["recurrences"] == 3, f"rc={rc} {out[:120]}")
    check("a dated key is written", f"recurrence_{TODAY}" in e, sorted(e)[:8])
    check("the note is the value", "retried and hung" in str(e.get(f"recurrence_{TODAY}", "")))
    check("an earlier dated key survives", e.get("recurrence_2026_09_01", "").startswith("An earlier"))
    check("the neighbour is untouched", load(f)["the-neighbour-that-must-not-move"]["recurrences"] == 7)

    # 2. The `  id:` spelling. Matching only `- id:` silently refuses these,
    #    and searching backwards for the previous "- " edits the entry ABOVE.
    f = fresh()
    rc, out = run(f, "id-is-not-the-first-key", "Second shape.")
    c = load(f)
    check("id-not-first entry is found", rc == 0 and c["id-is-not-the-first-key"]["recurrences"] == 1,
          f"rc={rc} {out[:120]}")
    check("the entry ABOVE it did not move",
          c["first-entry-is-the-item-marker"]["recurrences"] == 2,
          c["first-entry-is-the-item-marker"]["recurrences"])

    # 3. No counter yet is a first repeat, not an error. And the neighbouring
    #    fix_pattern holds a colon and an apostrophe — quoting must survive.
    f = fresh()
    before_fix = load(f)["entry-with-no-counter-at-all"]["fix_pattern"]
    rc, out = run(f, "entry-with-no-counter-at-all", "First time it came back.")
    e = load(f)["entry-with-no-counter-at-all"]
    check("a missing counter starts at 1", rc == 0 and e["recurrences"] == 1, f"rc={rc} {out[:120]}")
    check("awkward punctuation survives", e["fix_pattern"] == before_fix, e["fix_pattern"][:60])

    # 4. Twice in one day. Clobbering would turn the second event into none.
    f = fresh()
    run(f, "first-entry-is-the-item-marker", "Morning failure.")
    rc, out = run(f, "first-entry-is-the-item-marker", "Afternoon failure.")
    e = load(f)["first-entry-is-the-item-marker"]
    v = str(e.get(f"recurrence_{TODAY}", ""))
    check("same-day counter reaches 4", rc == 0 and e["recurrences"] == 4, e.get("recurrences"))
    check("both same-day notes are kept", "Morning" in v and "Afternoon" in v, v[:100])

    # 5. A note containing YAML metacharacters must not break the file.
    f = fresh()
    nasty = "It printed `key: value` and 'quoted' \"both ways\" — then: exploded #1"
    rc, out = run(f, "first-entry-is-the-item-marker", nasty)
    check("a hostile note still parses", rc == 0 and len(load(f)) == 4, f"rc={rc} {out[:160]}")
    check("the hostile note round-trips",
          "exploded #1" in str(load(f)["first-entry-is-the-item-marker"][f"recurrence_{TODAY}"]))

    # 6. Refusals.
    f = fresh()
    rc, out = run(f, "no-such-entry-anywhere", "note")
    check("an unknown id is refused", rc != 0 and "not found" in out, out[:100])
    rc, out = run(f, "first-entry-is-the-item-marker", "   ")
    check("an empty note is refused", rc != 0 and "counter again" in out, out[:100])
    check("a refused run wrote nothing", f.read_text() == CATALOG)

    # 7. The file is not reformatted. A whole-file dump would bury another
    #    session's uncommitted work in a 700-entry diff.
    f = fresh()
    run(f, "the-neighbour-that-must-not-move", "Touched deliberately.")
    a = CATALOG.splitlines()
    b = f.read_text().splitlines()
    moved = [ln for ln in a if ln not in b]
    check("only the counter line changed shape", moved == ["  recurrences: 7"], moved[:4])
    check("exactly two lines are new", len([ln for ln in b if ln not in a]) == 2,
          [ln for ln in b if ln not in a])

    print()
    if fails:
        print(f"FAIL — {len(fails)}/{ran[0]} checks red: {', '.join(fails)}")
        return 1
    print(f"PASS — {ran[0]}/{ran[0]} checks green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
