#!/usr/bin/env python3
"""Fixtures for recall.py — the pull half of the Recall loop.

Runs against a synthetic catalog in a temp dir (RECALL_CATALOG_DIR), never the
real one, so the suite cannot be perturbed by what the drain wrote overnight and
cannot perturb it back.

The load-bearing case is `prose-only entry is retrievable`. Auto-injection reads
only BACKTICKED tokens out of probe_when, so an entry written in plain prose is
dropped from its index entirely — 426 of 1375 entries in one measured corpus. If
recall inherits that limitation it is not the pull half, it is a second copy of
push.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Sibling scripts resolve through the same env var the rest of the system uses;
# the default is the repo's scripts/ dir, found relative to this file.
SCRIPT_DIR = Path(os.environ.get("RECALL_SKILL_DIR")
                  or Path(__file__).resolve().parent.parent / "scripts")
RECALL = SCRIPT_DIR / "recall.py"

FM = [
    {"id": "backtick-entry-with-code-tokens",
     "what": "A worktree node_modules symlink resolves workspace packages to the donor tree.",
     "fix_pattern": "Copy node_modules instead of symlinking.",
     "probe_when": ["`npm ci` inside a git worktree", "`node_modules` is a symlink"],
     "recurrences": 0},
    {"id": "recurring-entry",
     "what": "A shared mutable branch collides between parallel sessions.",
     "fix_pattern": "Use per-task worktrees.",
     "probe_when": ["two sessions share one checkout"],
     "recurrences": 3},
]
PF = [
    # Real shape from the corpus: this entry's body lives in `verified_failure`,
    # a field name no allow-list would have guessed. The corpus has 40+ distinct
    # field names and `id` is the only one present in every entry, so recall must
    # index whatever an entry called its body — not a fixed list of field names.
    {"id": "body-in-a-nonstandard-field",
     "verified_failure": "Four plan tests failed on a Pacific machine because the runner "
                         "inherited the ambient timezone and the assertion pinned a literal date string.",
     "fix": "Pin TZ=UTC in the test config, not in individual tests.",
     "probe_when": ["a suite passes on CI and fails locally with no code difference"],
     "recurrences": 0},
    {"id": "prose-only-entry-no-backticks",
     "what": "A dated follow-up written into a handoff never fires because its only delivery "
             "mechanism is a person rereading the document on the right day.",
     "fix_pattern": "Make it an invariant the system evaluates, or register it where the runtime raises it.",
     "probe_when": ["writing a where to pick up section with a date in it",
                    "a check whose only trigger is a person rereading a document"],
     "recurrences": 0},
    {"id": "entry-with-no-probe-when-at-all",
     "what": "Quarantine notes asserting items are unrecoverable decay over time.",
     "fix_pattern": "Re-verify by content before any bulk action.",
     "recurrences": 0},
]


def catalog_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="recalltest-"))
    import yaml
    (d / "FAILURE_MODES.yaml").write_text(yaml.safe_dump(FM, sort_keys=False))
    (d / "PROCESS_FAILURES.yaml").write_text(yaml.safe_dump(PF, sort_keys=False))
    return d


def run(d: Path, *args: str, probe_index: str | None = None) -> tuple[int, str]:
    env = {**os.environ, "RECALL_CATALOG_DIR": str(d),
           "RECALL_PROBE_INDEX": probe_index or str(d / "nonexistent-index.json")}
    p = subprocess.run([sys.executable, str(RECALL), *args],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    d = catalog_dir()
    fails = []

    ran = [0]

    def check(name, cond, detail=""):
        ran[0] += 1
        if cond:
            print(f"  [ok ] {name}")
        else:
            fails.append(f"{name}: {detail}")
            print(f"  [FAIL] {name}  {detail}")

    # THE case: prose with zero backticks must still be findable.
    rc, out = run(d, "a follow-up in a handoff nobody rereads", "-n", "3")
    check("prose-only entry is retrievable", "prose-only-entry-no-backticks" in out,
          f"rc={rc} out={out[:160]!r}")

    # An entry with NO probe_when at all is invisible to push; recall must see it.
    rc, out = run(d, "quarantine notes that claim things are unrecoverable")
    check("entry with no probe_when is retrievable", "entry-with-no-probe-when-at-all" in out)

    # Body stored under a field no allow-list would guess must still be searchable.
    rc, out = run(d, "runner inherited the ambient timezone")
    check("body in a nonstandard field is indexed", "body-in-a-nonstandard-field" in out,
          f"out={out[:160]!r}")

    # Pure metadata must not pollute the index (an ISO date is not content).
    rc, out = run(d, "2026")
    check("bare dates are not indexed as content", "no match" in out or "body-in-a-nonstandard" not in out)

    # Literal/code queries still work.
    rc, out = run(d, "node_modules symlink worktree")
    check("code-token query works", "backtick-entry-with-code-tokens" in out)

    # Sub-token splitting: querying one half of a compound finds it.
    rc, out = run(d, "symlink donor")
    check("compound tokens split for matching", "backtick-entry-with-code-tokens" in out)

    # Recurrence boost: a repeated lesson outranks an equal-relevance fresh one.
    rc, out = run(d, "--recurring", "--json")
    try:
        rows = json.loads(out)
        check("--recurring returns only repeated entries", rows and all(r["recurrences"] > 0 for r in rows))
    except Exception as e:
        check("--recurring returns only repeated entries", False, str(e))

    # --id exact lookup and its suggestion path.
    rc, out = run(d, "--id", "recurring-entry")
    check("--id finds an exact entry", rc == 0 and "per-task worktrees" in out)
    rc, out = run(d, "--id", "recurring")
    check("--id suggests near misses", rc == 1 and "did you mean" in out)

    # --stats must report the coverage gap honestly when no probe index exists.
    rc, out = run(d, "--stats", "--json")
    try:
        s = json.loads(out)
        expected = len(FM) + len(PF)   # derived, never hardcoded — a new fixture must not break this
        check("--stats counts every entry", s["entries"] == expected, f"got {s.get('entries')} want {expected}")
        check("--stats reports recall covers all", s["reachable_by_recall"] == s["entries"])
        check("--stats reports injection gap", s["unreachable_by_injection"] == expected,
              "with no probe index present, every entry is unreachable by injection")
    except Exception as e:
        check("--stats emits valid json", False, str(e))

    # No match must be graceful, not a crash or a false hit.
    rc, out = run(d, "zzzz nonexistent quantum flux capacitor")
    check("no-match is graceful", rc == 0 and "no match" in out)

    # Empty/stopword-only query must not return the whole corpus.
    rc, out = run(d, "the and of it")
    check("stopword-only query returns nothing", "no match" in out)

    # JSON mode is machine-parseable.
    rc, out = run(d, "--json", "handoff follow-up")
    try:
        rows = json.loads(out)
        check("--json emits a parseable list", isinstance(rows, list) and rows and "id" in rows[0])
    except Exception as e:
        check("--json emits a parseable list", False, str(e))

    # An unparseable catalog must DEGRADE, not crash. Load-bearing: the health
    # surface calls recall --stats to decide whether to report, so a traceback
    # here turned a broken catalog into total silence.
    import shutil as _sh
    _bd = Path(tempfile.mkdtemp(prefix="recallbroken-"))
    _sh.copy(d / "FAILURE_MODES.yaml", _bd / "FAILURE_MODES.yaml")
    (_bd / "PROCESS_FAILURES.yaml").write_text('- id: broken\n  what: "unclosed\n   bad: [1,2\n')
    rc, out = run(_bd, "--stats", "--json")
    check("broken catalog does not crash --stats", rc == 0, f"rc={rc}")
    try:
        _s = json.JSONDecoder().raw_decode(out[out.index("{"):])[0]
        check("broken catalog is named in --stats", bool(_s.get("unreadable_catalogs")))
        check("readable catalog still counted", _s.get("entries", 0) == len(FM))
    except Exception as e:
        check("broken catalog --stats emits json", False, str(e))
    rc, out = run(_bd, "node_modules symlink worktree")
    check("query still serves what parsed", rc == 0 and "did not parse" in out)
    _sh.rmtree(_bd, ignore_errors=True)

    # Missing catalog dir is an error, not a silent empty result.
    rc, out = run(d / "nonexistent-catalogs", "anything")
    check("missing catalogs errors loudly", rc == 2 and "no readable catalogs" in out)

    if fails:
        print(f"\nFAIL {len(fails)}/{ran[0]}")
        for f in fails:
            print("  -", f)
        return 1
    print(f"\nPASS {ran[0]}/{ran[0]} recall fixtures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
