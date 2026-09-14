#!/usr/bin/env python3
"""Run every suite. Exit non-zero if any is red.

Deliberately a plain runner with no framework: this has to work in a fresh clone
with nothing installed but PyYAML, which is the same constraint the rest of the
project holds itself to.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    suites = sorted(HERE.glob("test_*.py"))
    if not suites:
        print("no suites found", file=sys.stderr)
        return 1
    width = max(len(s.stem) for s in suites)
    red = []
    for s in suites:
        p = subprocess.run([sys.executable, str(s)], capture_output=True, text=True)
        last = (p.stdout.strip().splitlines() or ["(no output)"])[-1]
        print(f"  {s.stem:<{width}}  {last}")
        if p.returncode != 0:
            red.append((s.stem, p.stdout + p.stderr))
    print()
    if red:
        print(f"FAIL — {len(red)}/{len(suites)} suite(s) red\n")
        for name, out in red:
            print(f"--- {name} ---")
            print("\n".join(out.strip().splitlines()[-15:]))
            print()
        return 1
    print(f"PASS — {len(suites)}/{len(suites)} suites green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
