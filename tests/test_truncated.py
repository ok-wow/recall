#!/usr/bin/env python3
"""Fixtures for recall.py --truncated — the entries push cuts on the way out.

The push channel prints at most `build_probe_index.PUSH_CAP` characters of an
entry's body and the same of its "what to do" line. Anything past that fires,
looks delivered, and reaches the reader with its instruction missing. This is the third blind spot the tool
reports, and the only one where the entry is doing everything right.

Two cases decide whether the report is usable at all: it must find a cut that
removes an instruction, and it must stay quiet about a cut that removes a full
stop. A report that flags 'lost a period' gets switched off before it can show
anything real.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(os.environ.get("RECALL_SKILL_DIR")
                  or Path(__file__).resolve().parent.parent / "scripts")
RECALL = SCRIPT_DIR / "recall.py"
sys.path.insert(0, str(SCRIPT_DIR))
import build_probe_index as push  # noqa: E402

# Both the fixtures and the assertions read the LIVE cap. Sizing a fixture to
# cross a hardcoded threshold means the day the threshold moves, the fixture
# stops crossing it, the behaviour never fires, and four green checks turn red
# for a reason that has nothing to do with the behaviour. That is exactly what
# happened here when the cap went 400 -> 1200
# (FAILURE_MODES: config-limit-raise-breaks-tests-with-fixture-sized-to-old-limit).
CAP = push.PUSH_CAP
PAD = "word " * (CAP // 5 + 10)         # comfortably over the cap, whatever it is


def entry(eid, **fields):
    return {"id": eid, "recurrences": 0, **fields}


FM = [
    # Under the cap on both fields: must produce nothing.
    entry("short-entry-fits-in-the-window",
          summary="A short body.", fix_pattern="A short fix."),
    # The fix runs long and the tail is a real instruction.
    entry("fix-is-cut-mid-instruction",
          summary="A short body.",
          fix_pattern=PAD + "and then restart the worker pool before retrying."),
    # The body runs long instead.
    entry("body-is-cut-mid-sentence",
          summary=PAD + "and the second cause is the shared cache.",
          fix_pattern="A short fix."),
    # Over the cap by punctuation only: must stay quiet.
    entry("over-by-a-full-stop-only",
          summary="A short body.",
          fix_pattern="x" * CAP + "."),
]


def catalog_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="recalltrunc-"))
    import yaml
    (d / "FAILURE_MODES.yaml").write_text(yaml.safe_dump(FM, sort_keys=False))
    (d / "PROCESS_FAILURES.yaml").write_text(yaml.safe_dump([], sort_keys=False))
    return d


def run(d: Path, *args):
    env = {**os.environ, "RECALL_CATALOG_DIR": str(d),
           # Pin the last unpinned path too, or every fixture corpus silently
           # gains the real rules from ~/.claude/skills.
           "RECALL_SKILLS_DIR": str(Path(__file__).resolve().parent / "fixtures" / "no-skills"),
           "RECALL_PROBE_INDEX": str(d / "none.json"),
           # Every corpus recall can read has to be pinned here, or a loader
           # added later silently pulls a real store into a fixture-sized test.
           # Third time: specs/hub/connectors this morning, receipts/orphans now.
           "RECALL_RECEIPT_DIR": str(d / "none-receipts"),
           "RECALL_ORPHAN_INDEX": str(d / "none-orphans.yaml"),
           "RECALL_LOT_DIR": str(d / "none-lot"),
           "RECALL_SPECS_DIR": str(d / "none-specs-dir"),
           "RECALL_SURFACED_LOG": str(d / "surfaced.jsonl")}
    p = subprocess.run([sys.executable, str(RECALL), *args],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    d = catalog_dir()
    fails, ran = [], [0]

    def check(name, cond, detail=""):
        ran[0] += 1
        print(f"  [{'ok ' if cond else 'FAIL'}] {name}" + ("" if cond else f"  {detail}"))
        if not cond:
            fails.append(name)

    rc, out = run(d, "--truncated", "--json")
    check("exits 0", rc == 0, out[:200])
    found = json.loads(out)
    by = {f["id"]: f for f in found}

    check("an entry inside the window is not reported",
          "short-entry-fits-in-the-window" not in by, sorted(by))
    check("a cut that loses only a full stop is not reported",
          "over-by-a-full-stop-only" not in by, sorted(by))
    check("a fix cut mid-instruction is reported",
          "fix-is-cut-mid-instruction" in by, sorted(by))
    check("a body cut mid-sentence is reported",
          "body-is-cut-mid-sentence" in by, sorted(by))

    f = by.get("fix-is-cut-mid-instruction")
    if f:
        loss = max(f["losses"], key=lambda l: l["cut"])
        check("it names which part was cut", loss["part"] == "what to do", loss["part"])
        check("the tail is the text never shown",
              loss["tail"].endswith("restart the worker pool before retrying."),
              loss["tail"][-50:])
        # The whole point of deriving from push rather than restating its cap.
        shown = push.remedy(FM[1])
        check("the cut equals what push actually withheld",
              loss["cut"] == len(" ".join(FM[1]["fix_pattern"].split())) - len(shown),
              f"cut={loss['cut']}")
        check("push really did stop at the cap", len(shown) == CAP, len(shown))

    b = by.get("body-is-cut-mid-sentence")
    if b:
        check("a body loss is labelled body",
              max(b["losses"], key=lambda l: l["cut"])["part"] == "body")

    check("cheapest fix first", [f["worst"] for f in found] == sorted(f["worst"] for f in found),
          str([f["worst"] for f in found]))

    rc, out = run(d, "--truncated")
    check("the human report names the bands and the tail",
          "trim a clause" in out or "trim a sentence" in out, out[:160])
    check("it says what to do about it", "Shorten the field" in out, out[-160:])

    print()
    if fails:
        print(f"FAIL — {len(fails)}/{ran[0]} checks red: {', '.join(fails)}")
        return 1
    print(f"PASS — {ran[0]}/{ran[0]} checks green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
