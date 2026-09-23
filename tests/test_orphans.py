#!/usr/bin/env python3
"""The orphanage loader — recall.py load_orphans().

Only a parked orphan is unhomed learning. Once promoted, its knowledge lives at
promoted_to, and the parked text is the version from before verification: on
2026-09-22, 9 of 17 promoted code facts had changed and 2 were false. Indexing
promoted orphans would keep serving exactly what the promotion corrected.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

ORPHANAGE = """\
orphans:
- id: still-parked
  proposed_category: design-taste
  principle: a parked learning nobody has homed yet
  status: parked
- id: already-promoted
  proposed_category: architecture-fact
  principle: the backend has no MCP client
  status: promoted
  promoted_to: ~/dev/specs/_architecture/mcp-integration.md
- id: no-status-field
  proposed_category: org-domain
  principle: older entries carry no status and are parked by default
- id: cluster-rejected-one
  proposed_category: noise
  principle: rejected as noise
  status: cluster-rejected
clusters: []
"""


def main() -> int:
    fails, ran = [], [0]

    def check(name, cond, detail=""):
        ran[0] += 1
        print(f"  [{'ok ' if cond else 'FAIL'}] {name}" + ("" if cond else f"  {detail}"))
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "learnings.yaml"
        path.write_text(ORPHANAGE)
        os.environ["RECALL_ORPHAN_INDEX"] = str(path)
        spec = importlib.util.spec_from_file_location("recall_under_test", REPO / "scripts" / "recall.py")
        recall = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(recall)
        broken: list[dict] = []
        ids = {e["id"] for e in recall.load_orphans(broken)}

    check("the orphanage parses", not broken, str(broken))
    check("a parked orphan is indexed", "still-parked" in ids, str(ids))
    check("an orphan with no status counts as parked", "no-status-field" in ids, str(ids))
    check("a promoted orphan is NOT indexed", "already-promoted" not in ids, str(ids))
    check("a cluster-rejected orphan is NOT indexed", "cluster-rejected-one" not in ids, str(ids))

    print(f"\n{'FAIL' if fails else 'PASS'} {ran[0] - len(fails)}/{ran[0]} orphan-loader checks")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
