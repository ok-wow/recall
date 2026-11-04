#!/usr/bin/env python3
"""A write to the store commits itself -- scripts/store_commit.py.

Runs the real writers against a throwaway store (RECALL_HOME) that is its own
git repo. The assertions that matter: only the written path is committed, and a
catalog that lost an id since HEAD is refused rather than committed.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def sh(store: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(store), *args], capture_output=True, text=True).stdout


def run(store: Path, script: str, *args: str, stdin: str = "", **env) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], input=stdin, text=True,
                          capture_output=True, cwd=store,
                          env={**os.environ, "RECALL_HOME": str(store), **env})


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        store = Path(d)
        (store / "catalogs").mkdir()
        cat = store / "catalogs" / "PF.yaml"
        cat.write_text("entries:\n- id: alpha-entry\n  recurrences: 0\n- id: beta-entry\n  what: b\n")
        (store / "other.txt").write_text("base\n")
        for a in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                  ["add", "-A"], ["commit", "-qm", "base"]):
            sh(store, *a)
        (store / "other.txt").write_text("someone else's edit\n")

        r = run(store, "bump_recurrence.py", "--session", "t1", "catalogs/PF.yaml", "alpha-entry", "-",
                stdin="session t1 repeat")
        check("bump succeeds", r.returncode == 0, r.stderr)
        check("bump committed", "alpha-entry recurred" in sh(store, "log", "-1", "--format=%s"))
        check("an unrelated dirty file is not committed", " M other.txt" in sh(store, "status", "--short"))

        cat.write_text(cat.read_text().replace("- id: beta-entry\n  what: b\n", ""))
        head = sh(store, "rev-parse", "HEAD")
        r = run(store, "bump_recurrence.py", "--session", "t1", "catalogs/PF.yaml", "alpha-entry", "-",
                stdin="session t1 again")
        check("a lost id is refused", "lost 1 id" in r.stderr, r.stderr)
        check("nothing was committed", sh(store, "rev-parse", "HEAD") == head)
        sh(store, "checkout", "-q", "catalogs/PF.yaml")

        head = sh(store, "rev-parse", "HEAD")
        r = run(store, "bump_recurrence.py", "--session", "t1", "catalogs/PF.yaml", "alpha-entry", "-",
                stdin="session t1 off", RECALL_AUTOCOMMIT="0")
        check("RECALL_AUTOCOMMIT=0 writes but does not commit",
              r.returncode == 0 and sh(store, "rev-parse", "HEAD") == head, r.stderr)
        sh(store, "checkout", "-q", "catalogs/PF.yaml")

        r = run(store, "park.py", "add", "--session", "t1", "--title", "Item that should commit itself",
                "--body", "b", "--first-move", "f")
        check("park add succeeds", r.returncode == 0, r.stderr)
        check("park add committed", sh(store, "log", "-1", "--format=%s").startswith("lot: add"))

    print("PASS" if not FAILS else f"FAIL {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
