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

The copies are now ALLOWED to differ, in exactly one way: Codex sandboxes writes
outside its workspace, so it cannot write a learning receipt and must not be told
to. A plain diff would fail forever on that and get silenced within a week.

So the allowed delta is written here as an executable transform. Apply CODEX_DELTA
to the Claude copy and you must get the Codex copy byte for byte. Anything else is
drift. The transform IS the specification: to change what Codex is allowed to
differ by, you edit this list, which makes the exception reviewable instead of
invisible.

    check_hook_parity.py                 verify Codex = Claude + delta, and that
                                         every served hook IS the tracked file
    check_hook_parity.py --write-codex   DERIVE the Codex hook from the Claude copy
    check_hook_parity.py --link          replace every served copy with a link

The writer is the point of the transform, not a convenience on top of it. Because
the Codex copy is derivable, it does not need to be stored -- which matters, since
~/.codex/hooks is not a git repo and that file lives on exactly one machine. It
refuses rather than half-writes: if any passage CODEX_DELTA rewrites is missing
from the source, or the result loses the contract body, nothing is written.
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
            if (not src.is_file() or p == CODEX or ".bak-" in p.name
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


def render_codex(claude: str) -> str | None:
    """The Codex hook, derived from the Claude one. Returns None if the transform
    no longer applies -- a silently half-applied contract is worse than none."""
    out = claude
    for src, dst, _why in CODEX_DELTA:
        if src not in out:
            return None
        out = out.replace(src, dst, 1)
    return out


def write_codex(target: Path) -> int:
    """Regenerate the Codex hook from the Claude copy plus the declared delta.

    ~/.codex/hooks is not a git repo, so this file is stored nowhere. Deriving it
    is what makes it recoverable, and it removes the copy that drifts: there is
    one authored contract and one transform, never two files to keep in step.
    """
    if not CLAUDE.exists():
        print(f"hook-parity: cannot derive — no source at {CLAUDE}", file=sys.stderr)
        return 1

    rendered = render_codex(CLAUDE.read_text())
    if rendered is None:
        print("hook-parity: the Claude copy no longer contains every passage CODEX_DELTA "
              "rewrites, so the derived hook would be half-transformed. Refusing to "
              "write. Update CODEX_DELTA to match the current source.", file=sys.stderr)
        return 1
    # A contract that lost its body would still be valid shell, so check the body.
    if "Procedure 1" not in rendered or len(rendered) < 2000:
        print(f"hook-parity: derived hook looks wrong ({len(rendered)} bytes). "
              f"Refusing to write.", file=sys.stderr)
        return 1

    if target.exists() and target.read_text() == rendered:
        print(f"hook-parity: {target} already matches the derived contract — no change")
        return 0

    if target.exists():
        backup = target.with_suffix(target.suffix + f".bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(target, backup)
        print(f"hook-parity: backed up  {backup}")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"hook-parity: {target} did not exist — deriving it from scratch")

    target.write_text(rendered)
    target.chmod(target.stat().st_mode | 0o111)          # hooks are executed

    # A hook that does not parse silently stops serving the contract.
    bad = subprocess.run(["bash", "-n", str(target)], capture_output=True, text=True)
    if bad.returncode != 0:
        print(f"hook-parity: WROTE A FILE THAT DOES NOT PARSE — {bad.stderr.strip()}",
              file=sys.stderr)
        return 1

    print(f"hook-parity: wrote {target} ({len(rendered)} bytes, "
          f"{len(CODEX_DELTA)} transforms applied, bash -n clean)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="check_hook_parity")
    ap.add_argument("--write-codex", action="store_true",
                    help="derive the Codex hook from the Claude copy + CODEX_DELTA")
    ap.add_argument("--target", type=Path, default=CODEX,
                    help="where to write (tests point this at a temp file)")
    ap.add_argument("--link", action="store_true",
                    help="replace every served copy of a tracked hook with a link to it")
    a = ap.parse_args()

    if a.write_codex:
        return write_codex(a.target)
    if a.link:
        return link_all()

    for f in (CLAUDE, CODEX):
        if not f.exists():
            print(f"hook-parity: MISSING {f}", file=sys.stderr)
            return 1

    claude, codex = CLAUDE.read_text(), CODEX.read_text()

    expected = claude
    for src, dst, why in CODEX_DELTA:
        if src not in expected:
            print(f"hook-parity: the Claude copy no longer contains a passage this "
                  f"transform expects:\n    {src.strip()[:70]}…\n"
                  f"  Either the Claude hook changed and CODEX_DELTA needs updating, or "
                  f"the delta is stale.", file=sys.stderr)
            return 1
        expected = expected.replace(src, dst, 1)

    stale = unlinked()

    if expected == codex and not stale:
        print("hook-parity: Claude and Codex hooks agree "
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

    print("hook-parity: DRIFT — the Codex hook is not the Claude hook plus its "
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
