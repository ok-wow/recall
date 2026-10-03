#!/usr/bin/env python3
"""check_hook_parity.py — one copy of each hook, served everywhere by link.

On 2026-09-22 the committed mirror had drifted on 5 of 22 hooks and the Codex
copies on 5 of 22, one of them missing the drain's refuse-to-run-unattended
guard. The fix replaces copies with links, so there is nothing left to compare.
These checks run the real module against a throwaway $HOME.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_hook_parity.py"


def load(home: Path):
    os.environ["HOME"] = str(home)          # the module reads Path.home() at import
    spec = importlib.util.spec_from_file_location(f"hp_{id(home)}", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main() -> int:
    fails, ran = [], [0]
    real_home = os.environ.get("HOME", "")

    def check(name, cond, detail=""):
        ran[0] += 1
        print(f"  [{'ok ' if cond else 'FAIL'}] {name}" + ("" if cond else f"  {detail}"))
        if not cond:
            fails.append(name)

    try:
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            tracked = home / ".claude/skills/_hooks"; tracked.mkdir(parents=True)
            live = home / ".claude/hooks"; live.mkdir(parents=True)
            codex = home / ".codex/hooks"; codex.mkdir(parents=True)
            mirror = home / ".claude/skills/hooks"; mirror.mkdir(parents=True)
            for n in ("a.sh", "b.sh", "okwow-compound-userprompt.sh"):
                (tracked / n).write_text(f"#!/bin/sh\necho {n} v2\n")
            (live / "a.sh").symlink_to("../skills/_hooks/a.sh")          # already right
            (codex / "a.sh").write_text("#!/bin/sh\necho a.sh v2\n")      # identical copy
            (codex / "b.sh").write_text("#!/bin/sh\necho b.sh v1\n")      # drifted copy
            (codex / "okwow-compound-userprompt.sh").write_text("derived\n")  # the old derived copy
            (mirror / "b.sh").write_text("#!/bin/sh\necho b.sh v1\n")     # drifted, in repo
            (codex / "codex-only.sh").write_text("#!/bin/sh\n")           # no tracked twin

            m = load(home)
            found = {str(p.relative_to(home)): same for p, same in m.unlinked()}
            check("a correct link is not reported", ".claude/hooks/a.sh" not in found, found)
            check("an identical copy is reported as a copy", found.get(".codex/hooks/a.sh") is True, found)
            check("a drifted copy is reported as drifted", found.get(".codex/hooks/b.sh") is False, found)
            check("a drifted copy in the repo mirror is reported", ".claude/skills/hooks/b.sh" in found, found)
            check("the old derived Codex userprompt copy is reported like any copy",
                  found.get(".codex/hooks/okwow-compound-userprompt.sh") is False, found)
            check("a hook with no tracked twin is left alone", ".codex/hooks/codex-only.sh" not in found, found)

            m.link_all()
            check("after --link nothing is unlinked", m.unlinked() == [], m.unlinked())
            check("a Codex hook now resolves to the tracked file",
                  (codex / "b.sh").resolve() == (tracked / "b.sh").resolve())
            check("a link inside the repo is relative, so it works in any clone",
                  os.readlink(mirror / "b.sh") == "../_hooks/b.sh", os.readlink(mirror / "b.sh"))
            check("a link outside the repo is absolute",
                  os.readlink(codex / "b.sh") == str(tracked / "b.sh"), os.readlink(codex / "b.sh"))
            baks = sorted(p.name for p in codex.iterdir() if ".bak-" in p.name)
            check("drifted Codex copies are kept as .bak, the identical one is not",
                  sorted(b.split(".bak-")[0] for b in baks)
                  == ["b.sh", "okwow-compound-userprompt.sh"], baks)
            check("no .bak is written inside the repo (git holds it)",
                  not [p for p in mirror.iterdir() if ".bak-" in p.name])
            check("the Codex userprompt hook is now a link to the tracked script",
                  (codex / "okwow-compound-userprompt.sh").resolve()
                  == (tracked / "okwow-compound-userprompt.sh").resolve())
            check("no temp link is left behind", not [p for p in codex.iterdir() if ".link-" in p.name])

        # Output parity: one script, served from two places, must print the
        # Claude text for Claude and that text plus CODEX_DELTA for Codex.
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            tracked = home / ".claude/skills/_hooks"; tracked.mkdir(parents=True)
            (home / ".claude/hooks").mkdir(parents=True); (home / ".codex/hooks").mkdir(parents=True)
            # `cat` echoes the payload, so both hosts must be sent the same session.
            body = ("echo 'Procedure 1.'\ncat; echo\n"
                    "printf '4. Write a receipt, or add an immutable version to a receipt.\\n5. Verify each write.\\n'\n"
                    "echo 'After you verify the write, add the human-facing receipt contract at the end of your reply.'\n"
                    "printf '\\nHUMAN-FACING RECEIPT CONTRACT\\nokWOW captured <id> in <path>.\\n'\n")
            good = ("#!/bin/bash\nf() { cat; }\ncase \"${BASH_SOURCE[0]}\" in */.codex/*) f() { sed "
                    "-e '/^4\\. Write a receipt/d' -e 's/^5\\. Verify/4. Verify/' "
                    "-e 's/^After you verify.*/Do not write a learning receipt. Do not mention receipts in your reply./' "
                    "-e '/^HUMAN-FACING/d' -e '/^okWOW captured/d'; } ;; esac\n{\n" + body + "} | f\n")
            hook = tracked / "okwow-compound-userprompt.sh"
            hook.write_text(good)
            (home / ".claude/hooks/okwow-compound-userprompt.sh").symlink_to(hook)
            (home / ".codex/hooks/okwow-compound-userprompt.sh").symlink_to(hook)
            m = load(home)
            check("a host-aware script served from both places passes", m.main() == 0)
            hook.write_text("#!/bin/bash\n" + body)
            check("the same text for both hosts is drift", m.main() == 1)
    finally:
        os.environ["HOME"] = real_home

    print(f"\n{'FAIL' if fails else 'PASS'} {ran[0] - len(fails)}/{ran[0]} hook-parity checks")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
