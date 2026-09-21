#!/usr/bin/env python3
"""Pointing an install at this repo — scripts/link_scripts.sh.

Copies drift. This repo and the installed okwow-compound skill held five of the
same scripts and diverged in BOTH directions, far enough that three PRs landed
against a tree nothing invoked. The catalogued entry
per-host-copies-drift-until-something-compares-them had fired twice before that
and its fix is the one implemented here: a symlink, so there is nothing to
compare.

What these cases actually guard is the SWAP. Remove-then-link leaves the
registered path missing in between, which is the gap the link exists to close,
and a hook that fires in that window fails with no trace. So the swap is
link-beside-then-mv-over, and the test that matters asserts the target is never
absent and never empty.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LINKER = REPO / "scripts" / "link_scripts.sh"


def run(target: Path, *args: str) -> tuple[int, str]:
    p = subprocess.run(["bash", str(LINKER), str(target), *args],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    fails, ran = [], [0]

    def check(name, cond, detail=""):
        ran[0] += 1
        print(f"  [{'ok ' if cond else 'FAIL'}] {name}" + ("" if cond else f"  {detail}"))
        if not cond:
            fails.append(name)

    shipped = sorted(p.name for p in (REPO / "scripts").glob("*.py"))

    # 1. A dry run reports and writes nothing. A tool that edits a live install
    #    when asked to describe itself is not one anybody will run twice.
    d = Path(tempfile.mkdtemp(prefix="recalllink-"))
    (d / "recall.py").write_text("# an installed copy\n")
    before = (d / "recall.py").read_text()
    rc, out = run(d)
    check("dry run exits clean", rc == 0, f"rc={rc}")
    check("dry run says it is a dry run", "dry run" in out, out[-90:])
    check("dry run changes nothing",
          (d / "recall.py").read_text() == before and not (d / "recall.py").is_symlink())

    # 2. --apply links every shipped script, and the links resolve.
    rc, out = run(d, "--apply")
    check("apply exits clean", rc == 0, f"rc={rc}")
    linked = [n for n in shipped if (d / n).is_symlink()]
    check("every shipped script is linked", linked == shipped,
          f"{len(linked)}/{len(shipped)}")
    check("every link resolves to this repo",
          all((d / n).resolve() == (REPO / "scripts" / n).resolve() for n in shipped))

    # 3. A differing local copy is backed up, not silently destroyed. A
    #    difference between two trees is information — it is what this whole
    #    merge was reconstructed from.
    baks = list(d.glob("recall.py.pre-link-*"))
    check("a differing copy is backed up first", len(baks) == 1, [b.name for b in baks])
    check("the backup holds the original", baks and baks[0].read_text() == before)

    # 4. Idempotent: running again relinks nothing and reports so.
    rc, out = run(d, "--apply")
    check("second run is a no-op", rc == 0 and f"{len(shipped)} already linked" in out,
          out[-90:])
    check("no second backup", len(list(d.glob("recall.py.pre-link-*"))) == 1)

    # 5. THE SWAP. The target must never be absent or empty — not for an
    #    instant. Removing first and linking second would pass every check
    #    above and still drop every hook that fired in the gap, so this asserts
    #    the mechanism rather than the outcome: the script must create a temp
    #    link and mv it over, never rm the live path.
    src = LINKER.read_text()
    check("the swap is mv-over, not rm-then-link",
          "mv -f" in src and "rm -f \"$dst\"" not in src and "rm \"$dst\"" not in src)
    d2 = Path(tempfile.mkdtemp(prefix="recalllink2-"))
    (d2 / "recall.py").write_text("x")
    run(d2, "--apply")
    check("the target exists and is non-empty after the swap",
          (d2 / "recall.py").exists() and (d2 / "recall.py").stat().st_size > 0)

    # 6. Refuses a target that is not there, rather than creating one. An
    #    install that does not exist is a typo, not a request.
    rc, out = run(Path(d / "nope"), "--apply")
    check("a missing target is refused", rc != 0 and "no such directory" in out, out[:80])

    # 7. Opportunistic: if this machine has the skill installed, report whether
    #    it has drifted. Never fails the suite on it — a developer machine with
    #    a deliberately patched install is not a broken repo.
    live = Path.home() / ".claude/skills/okwow-compound/scripts"
    if live.is_dir():
        drift = [n for n in shipped
                 if (live / n).is_file() and not (live / n).is_symlink()
                 and (live / n).read_bytes() != (REPO / "scripts" / n).read_bytes()]
        missing = [n for n in shipped if not (live / n).exists()]
        print(f"\n  install at {live}:")
        print(f"    {sum(1 for n in shipped if (live / n).is_symlink())} linked, "
              f"{len(drift)} drifted, {len(missing)} not installed")
        if drift:
            print(f"    drifted: {', '.join(drift[:6])}")

    print()
    if fails:
        print(f"FAIL — {len(fails)}/{ran[0]} checks red: {', '.join(fails)}")
        return 1
    print(f"PASS — {ran[0]}/{ran[0]} checks green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
