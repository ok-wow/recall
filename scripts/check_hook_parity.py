#!/usr/bin/env python3
"""Fail loudly when the Claude and Codex compound hooks drift apart.

WHY THIS EXISTS.

The two hooks are the same contract served to two agents. On 2026-09-15 the Codex
copy was found missing 639 bytes: the entire body of Step 2 -- the recall.py
command, the dated-recurrence-key rule, and the fact that auto-injection cannot
see 43% of the corpus. It read only "Search the existing artifacts before you
create an artifact." So on Codex the agent was told to search and never told how,
never learned that duplicates become dated recurrences, and never learned why
search is not optional. Nothing compared the files, so nothing noticed.

That is PROCESS_FAILURES::per-host-copies-drift-until-something-compares-them,
recorded by this same project, against these same two files.

WHY IT IS NOT JUST `diff`.

The two hosts are ALLOWED to see different text, in exactly one way: Codex
sandboxes writes outside its workspace, so it cannot write a learning receipt and
must not be told to.

So the allowed delta is written here as an executable transform. Apply CODEX_DELTA
to what the hook prints for Claude and you must get what it prints for Codex, byte
for byte. Anything else is drift. The transform IS the specification: to change
what Codex is allowed to differ by, you edit this list, which makes the exception
reviewable instead of invisible.

Since 2026-10-03 there is one script. ~/.codex/hooks/okwow-compound-userprompt.sh
is a link like every other served hook, and the script applies the delta itself
when it is served from ~/.codex. The derived copy it replaced was a second file to
keep in step, and it was the only served hook that was not the tracked one.

    check_hook_parity.py           verify Codex output = Claude output + delta, and
                                   that every served hook IS the tracked file
    check_hook_parity.py --link    replace every served copy with a link
"""
from __future__ import annotations

import argparse
import difflib
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

CLAUDE = Path.home() / ".claude/hooks/okwow-compound-userprompt.sh"
CODEX = Path.home() / ".codex/hooks/okwow-compound-userprompt.sh"

# ONE COPY, NOT MATCHING COPIES. Until 2026-09-19 ~/.claude/hooks held real files
# and ~/.claude/skills/hooks held a committed mirror of two of them. Then the live
# hooks became symlinks into ~/.claude/skills/_hooks, so the tracked file IS what
# is served -- and every other copy became the drift this script exists to catch.
# Measured 2026-09-22: the mirror had drifted on 5 of 22 files (only 2 watched),
# and ~/.codex/hooks, which held plain copies, on 5 of 22 -- 475 lines behind on
# sessionstart, and without the drain's refuse-to-run-unattended guard. So the
# check is no longer "these copies agree". It is "every place a hook is served
# from resolves to the tracked file", except the hook DERIVED from it below.
TRACKED = Path.home() / ".claude/skills/_hooks"
SERVED_DIRS = [Path.home() / ".claude/hooks", Path.home() / ".codex/hooks",
               Path.home() / ".claude/skills/hooks"]
IN_REPO = Path.home() / ".claude/skills"   # links inside the repo stay relative


def unlinked() -> list[tuple[Path, bool]]:
    """(served path, identical?) for every served hook that is not the tracked file."""
    out = []
    for d in SERVED_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            src = TRACKED / p.name
            if (not src.is_file() or ".bak-" in p.name
                    or p.resolve() == src.resolve()):
                continue
            out.append((p, p.is_file() and p.read_bytes() == src.read_bytes()))
    return out


def link_all() -> int:
    """Replace every served copy with a link to the tracked file.

    A drifted copy is kept beside it as .bak-<time> first, since it may hold an
    edit nobody committed. The swap is link-beside-then-rename, so the served path
    is never missing -- a hook that fires in that gap would fail without a trace.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    todo = unlinked()
    for p, same in todo:
        src = TRACKED / p.name
        # Outside the repo a drifted copy may be the only record of an edit; inside
        # it, git already holds every committed version.
        if not same and IN_REPO not in p.parents:
            shutil.copy2(p, p.with_name(f"{p.name}.bak-{stamp}"))
        dest = (os.path.relpath(src, p.parent) if IN_REPO in p.parents else str(src))
        tmp = p.with_name(f".{p.name}.link-{stamp}")
        os.symlink(dest, tmp)
        os.replace(tmp, p)
        print(f"hook-parity: linked {p} -> {dest}" + ("" if same or IN_REPO in p.parents else "  (old copy kept as .bak)"))
    print(f"hook-parity: {len(todo)} served cop{'y' if len(todo) == 1 else 'ies'} replaced with links")
    return 0

# (what the Claude copy says, what the Codex copy must say instead, why)
CODEX_DELTA: list[tuple[str, str, str]] = [
    (
        "4. Write a receipt, or add an immutable version to a receipt.\n5. Verify each write.\n",
        "4. Verify each write.\n",
        "Codex cannot write to ~/.okwow/local-learning-receipts -- it is outside the "
        "workspace and the sandbox denies it. Instructing it to anyway left the agent "
        "holding an impossible step, and the bookkeeping surfaced in the user's reply.",
    ),
    (
        "After you verify the write, add the human-facing receipt contract at the end of your reply.\n",
        "Do not write a learning receipt. Do not mention receipts in your reply.\n",
        "No receipt can exist on this host, so there is nothing to report. Silence is "
        "the honest output, and an explicit prohibition beats an absent instruction.",
    ),
    (
        "\nHUMAN-FACING RECEIPT CONTRACT\nokWOW captured <id> in <path>.\n",
        "\n",
        "The receipt copy has no referent on Codex. Left in, the agent emits internal "
        "housekeeping to the user.",
    ),
]


def served_outputs() -> tuple[str, str]:
    """What the hook prints on a session's first prompt, served from each host.

    Same input, a fresh session and an empty once-per-session store for both, and
    no lot, so the only difference left is the host the script sees.
    """
    import tempfile
    import uuid
    outs = []
    payload = '{"session_id":"parity-%s"}' % uuid.uuid4().hex[:12]
    for path in (CLAUDE, CODEX):
        # One session id for both; a store each, so neither sees the other's first prompt.
        with tempfile.TemporaryDirectory() as once:
            env = {**os.environ, "OKWOW_HOOK_ONCE_DIR": once,
                   "OKWOW_PARK": str(Path(once) / "no-park.py")}
            p = subprocess.run(["bash", str(path)], input=payload, capture_output=True,
                               text=True, env=env, timeout=10)
            outs.append(p.stdout)
    return outs[0], outs[1]


def main() -> int:
    ap = argparse.ArgumentParser(prog="check_hook_parity")
    ap.add_argument("--write-codex", action="store_true",
                    help="retired: the Codex hook is a link now; same as --link")
    ap.add_argument("--link", action="store_true",
                    help="replace every served copy of a tracked hook with a link to it")
    a = ap.parse_args()

    if a.link or a.write_codex:
        return link_all()

    for f in (CLAUDE, CODEX):
        if not f.exists():
            print(f"hook-parity: MISSING {f}", file=sys.stderr)
            return 1

    claude, codex = served_outputs()

    expected = claude
    for src, dst, why in CODEX_DELTA:
        if src not in expected:
            print(f"hook-parity: the Claude output no longer contains a passage this "
                  f"transform expects:\n    {src.strip()[:70]}…\n"
                  f"  Either the Claude hook changed and CODEX_DELTA needs updating, or "
                  f"the delta is stale.", file=sys.stderr)
            return 1
        expected = expected.replace(src, dst, 1)

    stale = unlinked()

    if expected == codex and not stale:
        print("hook-parity: Claude and Codex output agree "
              f"({len(CODEX_DELTA)} declared differences); every served hook is "
              f"the tracked file")
        return 0

    if stale:
        print("hook-parity: a served hook is a COPY of the tracked one, not a link "
              "to it:", file=sys.stderr)
        for p, same in stale:
            print(f"  - {p}: " + ("identical today, and it will drift" if same
                                  else "DRIFTED from the tracked hook"), file=sys.stderr)
        print("  Fix:  python3 ~/.claude/skills/okwow-compound/scripts/"
              "check_hook_parity.py --link", file=sys.stderr)
        if expected == codex:
            return 1
        print(file=sys.stderr)

    print("hook-parity: DRIFT — the Codex output is not the Claude output plus its "
          "declared differences\n", file=sys.stderr)
    diff = difflib.unified_diff(
        expected.splitlines(keepends=True), codex.splitlines(keepends=True),
        fromfile="claude+declared-delta", tofile="codex-actual", n=1)
    sys.stderr.writelines(diff)
    print("\n  Sync it, or declare the new difference in CODEX_DELTA with its reason.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
