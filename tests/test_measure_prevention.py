#!/usr/bin/env python3
"""Fixtures for measure_prevention.py — the scoreboard Recall's README promises.

The README says the count of lessons that repeated ANYWAY is the honest measure
of compounding. That is half true and the missing half inverts the conclusion: a
repeat where the lesson was never delivered is a RETRIEVAL failure, and a repeat
where it was delivered is a HEEDING failure. They need opposite fixes, so a tool
that cannot separate them produces a number nobody can act on.

Every assertion below exists to stop that separation quietly breaking:

  * a fire AFTER the recurrence must not count as prevention (an off-by-one here
    turns every coverage failure into a heeding failure and reverses the advice)
  * test-harness rows must never count as production delivery -- this project
    once published a baseline where 45% of the evidence was fixtures
  * an undated bump must be reported as UNMEASURABLE, never silently dropped,
    because a denominator that shrinks to the convenient cases is the exact
    failure the corpus exists to catch
  * both spellings of the field must be read; `recurrence:` entries were
    invisible to the original reader
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "measure_prevention.py"

fails: list[str] = []
ran = [0]


def check(label: str, ok: bool, detail: str = "") -> None:
    ran[0] += 1
    if not ok:
        fails.append(f"{label}: {detail}")
    print(f"  [{'ok ' if ok else 'FAIL'}] {label}" + ("" if ok else f"  {detail}"))


def run(d: Path, *args: str) -> tuple[int, dict | str]:
    env = {**os.environ,
           "RECALL_CATALOG_DIR": str(d / "catalogs"),
           # Pin the last unpinned path too, or every fixture corpus silently
           # gains the real rules from ~/.claude/skills.
           "RECALL_SKILLS_DIR": str(Path(__file__).resolve().parent / "fixtures" / "no-skills"),
           "RECALL_SURFACED_LOG": str(d / "surfaced.jsonl"),
           "RECALL_PROBE_INDEX": str(d / "no-index.json"),
           "RECALL_HOME": str(d / "fake-home")}
    p = subprocess.run([sys.executable, str(SCRIPT), *args],
                       capture_output=True, text=True, env=env)
    if "--json" in args and p.returncode == 0:
        try:
            return p.returncode, json.loads(p.stdout)
        except Exception:
            return p.returncode, p.stdout + p.stderr
    return p.returncode, p.stdout + p.stderr


def build(d: Path, catalogs: dict, fires: list[dict]) -> None:
    (d / "catalogs").mkdir(parents=True, exist_ok=True)
    import yaml
    for name in ("FAILURE_MODES", "PROCESS_FAILURES", "DECISIONS"):
        (d / "catalogs" / f"{name}.yaml").write_text(
            yaml.safe_dump(catalogs.get(name, []), sort_keys=False))
    (d / "surfaced.jsonl").write_text(
        "".join(json.dumps(f) + "\n" for f in fires))


def main() -> int:
    tmp = Path(tempfile.mkdtemp())

    # --- the core split -------------------------------------------------------
    d = tmp / "split"
    build(d, {"FAILURE_MODES": [
        {"id": "shown-first", "recurrences": 1, "recurrence_2026_09_14": "again"},
        {"id": "never-shown", "recurrences": 1, "recurrence_2026_09_14": "again"},
    ]}, [
        {"ts": "2026-09-13T10:00:00Z", "session": "cli", "entry": "FM:shown-first"},
        {"ts": "2026-09-15T10:00:00Z", "session": "cli", "entry": "FM:never-shown"},
    ])
    rc, out = run(d, "--json")
    v = out["summary"]["verdicts"] if isinstance(out, dict) else {}
    check("a fire BEFORE the recurrence is surfaced-then-recurred",
          v.get("surfaced-then-recurred") == 1, str(v))
    # The load-bearing one. A fire on 09-15 cannot have prevented a recurrence on
    # 09-14; counting it would relabel a coverage failure as a heeding failure and
    # send the reader to rewrite an entry instead of fixing retrieval.
    check("a fire AFTER the recurrence does NOT count as prevention",
          v.get("never-surfaced") == 1, str(v))

    # --- fixtures are not evidence -------------------------------------------
    d = tmp / "fixtures"
    build(d, {"FAILURE_MODES": [
        {"id": "only-a-test-saw-it", "recurrences": 1, "recurrence_2026_09_14": "again"},
    ]}, [
        {"ts": "2026-09-13T10:00:00Z", "session": "test-harness-1", "entry": "FM:only-a-test-saw-it"},
        {"ts": "2026-09-13T10:00:00Z", "session": "regress-7", "entry": "FM:only-a-test-saw-it"},
        # Two unrelated PRODUCTION fires bracket 09-14. Without them the window
        # ends at 09-13 and the recurrence is `outside-log-window` — correct, but
        # it would mask what this case is actually asserting. The window is built
        # from non-test fires only, which is itself the property under test.
        {"ts": "2026-09-13T10:00:00Z", "session": "cli", "entry": "FM:something-else"},
        {"ts": "2026-09-15T10:00:00Z", "session": "cli", "entry": "FM:something-else"},
    ])
    rc, out = run(d, "--json")
    v = out["summary"]["verdicts"] if isinstance(out, dict) else {}
    check("a test-harness fire is not production delivery",
          v.get("never-surfaced") == 1 and not v.get("surfaced-then-recurred"), str(v))

    # --- a held-back fire is the control arm, never delivery --------------------
    d = tmp / "heldback"
    build(d, {"FAILURE_MODES": [
        {"id": "held-back-lesson", "recurrences": 1, "recurrence_2026_09_14": "again"},
    ]}, [
        {"ts": "2026-09-13T10:00:00Z", "session": "s1", "entry": "FM:held-back-lesson",
         "held_back": True},
        {"ts": "2026-09-13T10:00:00Z", "session": "cli", "entry": "FM:something-else"},
        {"ts": "2026-09-15T10:00:00Z", "session": "cli", "entry": "FM:something-else"},
    ])
    rc, out = run(d, "--json")
    v = out["summary"]["verdicts"] if isinstance(out, dict) else {}
    check("a held-back fire is not delivery",
          v.get("never-surfaced") == 1 and not v.get("surfaced-then-recurred"), str(v))

    # --- the unmeasurable bucket is reported, not dropped ---------------------
    d = tmp / "undated"
    build(d, {"FAILURE_MODES": [{"id": "bare-counter", "recurrences": 4}]},
          [{"ts": "2026-09-13T10:00:00Z", "session": "cli", "entry": "FM:other"}])
    rc, out = run(d, "--json")
    s = out["summary"] if isinstance(out, dict) else {}
    check("undated bumps are counted as unmeasurable, not discarded",
          s.get("verdicts", {}).get("undated-unmeasurable") == 4, str(s.get("verdicts")))
    check("the denominator includes what cannot be measured",
          s.get("recurrence_events") == 4 and s.get("measurable") == 0, str(s.get("measurable")))

    # --- both field spellings -------------------------------------------------
    d = tmp / "spelling"
    build(d, {"PROCESS_FAILURES": [
        {"id": "older-schema", "recurrence": ["a", "b"], "recurrence_2026_09_14": "again"},
    ]}, [{"ts": "2026-09-13T10:00:00Z", "session": "cli", "entry": "PF:older-schema"}])
    rc, out = run(d, "--json")
    s = out["summary"] if isinstance(out, dict) else {}
    check("`recurrence:` list spelling is read, not skipped",
          s.get("recurrence_events") == 2, str(s.get("recurrence_events")))

    # --- decisions are in scope ----------------------------------------------
    d = tmp / "decisions"
    build(d, {"DECISIONS": [
        {"id": "a-reversal", "recurrences": 1, "recurrence_2026_09_14": "revisited"},
    ]}, [{"ts": "2026-09-13T10:00:00Z", "session": "cli", "entry": "DE:a-reversal"}])
    rc, out = run(d, "--json")
    s = out["summary"] if isinstance(out, dict) else {}
    check("DECISIONS.yaml is measured too",
          s.get("recurrence_events") == 1, str(s.get("recurrence_events")))

    # --- a fresh install is not an error -------------------------------------
    d = tmp / "empty"
    build(d, {}, [])
    rc, out = run(d)
    check("an empty corpus exits 0 and explains itself",
          rc == 0 and "expected state for a new memory" in str(out), f"rc={rc}")
    rc, out = run(d, "--gaps")
    check("--gaps on an empty corpus is a clean no-op", rc == 0, f"rc={rc}")

    # --- never writes anything -----------------------------------------------
    d = tmp / "readonly"
    build(d, {"FAILURE_MODES": [{"id": "x", "recurrences": 1, "recurrence_2026_09_14": "y"}]},
          [{"ts": "2026-09-13T10:00:00Z", "session": "cli", "entry": "FM:x"}])
    run(d, "--json")
    check("an analyser writes nothing — least of all into the log it reads",
          not (d / "fake-home").exists(), "fake-home was created")

    if fails:
        print(f"\nFAIL {len(fails)}/{ran[0]}")
        for f in fails:
            print("  -", f)
        return 1
    print(f"\nPASS {ran[0]}/{ran[0]} prevention-measurement fixtures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
