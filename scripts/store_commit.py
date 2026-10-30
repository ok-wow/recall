#!/usr/bin/env python3
"""Commit a write to the Recall store the moment it lands.

A store whose commit step is separate from its write step drifts: writes pile
up uncommitted, and one bad overwrite loses them with no history. The writers
call this right after they write, so every change is committed on its own.

    store_commit.py -m "<message>" PATH [PATH ...]

Library use: commit_paths([paths], message) -> (committed, note).

It commits only the named paths, never the rest of the working tree, so
unrelated edits in progress are not swept in. It never pushes. It refuses a
catalog that lost an id since HEAD (two writers raced and one overwrote the
other) and anything that looks like a credential. A path outside the store, a store that is not its own repo, or RECALL_AUTOCOMMIT=0
makes it a no-op, so tests and fixtures are never committed anywhere.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from personal_repo import SECRET_NAME, SECRET_TEXT, STORE, git, is_own_repo  # noqa: E402

ID_LINE = re.compile(r"^\s*-?\s*id:\s*['\"]?([^\s'\"#]+)", re.M)


def _ids(text: str) -> set[str]:
    return set(ID_LINE.findall(text))


def _in_store(p: Path) -> Path | None:
    real = p.expanduser().resolve()
    try:
        return real.relative_to(STORE.resolve())
    except ValueError:
        return None


def commit_paths(paths, message: str) -> tuple[bool, str]:
    if os.environ.get("RECALL_AUTOCOMMIT", "1") == "0":
        return False, "autocommit off (RECALL_AUTOCOMMIT=0)"
    if not is_own_repo(STORE):
        return False, f"{STORE} is not its own git repo"
    rels = []
    for p in paths:
        rel = _in_store(Path(p))
        if rel is None or not (STORE / rel).exists():
            continue
        rels.append(str(rel))
    if not rels:
        return False, "no path inside the store"

    for rel in rels:
        if SECRET_NAME.search(rel):
            return False, f"refused: {rel} is named like a credential"
        data = (STORE / rel).read_bytes()
        if SECRET_TEXT.search(data):
            return False, f"refused: {rel} contains something shaped like a credential"
        if rel.endswith((".yaml", ".yml")):
            head = git(STORE, "show", f"HEAD:{rel}")
            if head.returncode == 0:
                lost = _ids(head.stdout) - _ids(data.decode("utf-8", "replace"))
                if lost:
                    return False, f"refused: {rel} lost {len(lost)} id(s) since HEAD: {sorted(lost)[:5]}"

    # Another writer may hold the index lock for a moment; wait it out briefly.
    for attempt in range(5):
        add = git(STORE, "add", "--", *rels)
        if add.returncode == 0:
            c = git(STORE, "commit", "-q", "-m", message, "--only", "--", *rels)
            if c.returncode == 0:
                sha = git(STORE, "rev-parse", "--short", "HEAD").stdout.strip()
                return True, f"committed {sha} ({len(rels)} file(s))"
            err = c.stdout + c.stderr
            if "nothing to commit" in err or "no changes added" in err:
                return False, "nothing to commit"
        else:
            err = add.stderr
        if "index.lock" not in err:
            return False, f"git failed: {err.strip()[:300]}"
        time.sleep(0.4 * (attempt + 1))
    return False, "git index stayed locked"


def report(paths, message: str) -> None:
    """For writers: commit, and say so on stderr only when it did not happen."""
    ok, note = commit_paths(paths, message)
    if not ok and not note.startswith(("autocommit off", "no path inside", "nothing to commit")):
        sys.stderr.write(f"store_commit: not committed: {note}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-m", "--message", required=True)
    ap.add_argument("paths", nargs="+")
    a = ap.parse_args()
    ok, note = commit_paths(a.paths, a.message)
    print(("ok  " if ok else "no  ") + note)
    return 0 if ok or note == "nothing to commit" else 1


if __name__ == "__main__":
    raise SystemExit(main())
