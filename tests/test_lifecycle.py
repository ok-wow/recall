#!/usr/bin/env python3
"""Retirement changes delivery while preserving explicit historical access."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml
from test_recall import run, SCRIPT_DIR


def main():
    failures = []
    checks = 0

    def check(name, ok):
        nonlocal checks
        checks += 1
        if not ok:
            failures.append(name)

    with tempfile.TemporaryDirectory(prefix="recall-lifecycle-") as folder:
        root = Path(folder)
        records = [
            {"id": "active", "what": "quartzmarker delivery lesson", "fix": "Use the active remedy.",
             "probe_when": ["`quartzmarker-active`"]},
            {"id": "retired", "status": "retired", "what": "quartzmarker delivery lesson",
             "fix": "An obsolete remedy.", "probe_when": ["`quartzmarker-retired`"]},
            {"id": "superseded", "status": "superseded", "what": "quartzmarker delivery lesson",
             "fix": "A replaced remedy.", "probe_when": ["`quartzmarker-superseded`"],
             "superseded_by": "active"},
            {"id": "archived", "status": "archived", "what": "quartzmarker delivery lesson",
             "fix": "Archived history.", "probe_when": ["`quartzmarker-archived`"]},
            {"id": "removed", "status": " ReMoVeD ", "what": "quartzmarker delivery lesson",
             "fix": "Removed history.", "probe_when": ["`quartzmarker-removed`"]},
            {"id": "deprecated-topic", "status": "deprecated", "what": "quartzmarker delivery lesson",
             "fix": "Still applicable advice about deprecated code.", "probe_when": ["`quartzmarker-deprecated`"]},
        ]
        (root / "FAILURE_MODES.yaml").write_text(yaml.safe_dump(records))
        (root / "PROCESS_FAILURES.yaml").write_text("[]\n")
        code, output = run(root, "--json", "quartzmarker", "-n", "10")
        found = {e["id"] for e in json.loads(output)} if code == 0 else set()
        check("ordinary search excludes inactive lessons", found == {"active", "deprecated-topic"})
        code, output = run(root, "--id", "retired", "--json")
        check("explicit id lookup preserves history", code == 0 and json.loads(output)["id"] == "retired")
        code, output = run(root, "--include-retired", "--json", "quartzmarker", "-n", "10")
        check("explicit archive search includes history", code == 0 and
              {e["id"] for e in json.loads(output)} == {e["id"] for e in records})
        rows = {e["id"]: e for e in json.loads(output)} if code == 0 else {}
        check("archive rows say which lessons are inactive", rows.get("retired", {}).get("status") == "retired"
              and rows.get("superseded", {}).get("superseded_by") == "active")
        check("archive rows leave active lessons unmarked", "status" not in rows.get("active", {"status": 1}))
        code, output = run(root, "--json", "quartzmarker", "-n", "10")
        check("ordinary rows carry no lifecycle fields", code == 0 and
              all("status" not in e and "superseded_by" not in e for e in json.loads(output)))
        code, output = run(root, "--id", "superseded")
        check("id lookup names the replacement", code == 0 and "SUPERSEDED by active" in output)
        code, output = run(root, "--id", "retired")
        check("id lookup says a lesson is retired", code == 0 and "RETIRED" in output)
        code, output = run(root, "--include-retired", "quartzmarker", "-n", "10")
        check("archive search marks inactive lessons", code == 0 and "ARCHIVED" in output
              and "SUPERSEDED by active" in output)
        code, output = run(root, "--id", "active")
        check("an active lesson has no lifecycle marker", code == 0 and not any(
              w in output for w in ("RETIRED", "SUPERSEDED", "ARCHIVED", "REMOVED")))
        code, output = run(root, "--stats", "--json")
        stats = json.loads(output) if code == 0 else {}
        check("coverage denominator counts active catalog only", stats.get("catalog_entries") == 2)
        check("stats retain the inactive population", stats.get("inactive_catalog_entries") == 4)

        env = {**os.environ, "RECALL_HOME": str(root), "RECALL_CATALOG_DIR": str(root),
               "RECALL_PROBE_INDEX": str(root / "index.json"), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, str(SCRIPT_DIR / "build_probe_index.py")],
                                env=env, capture_output=True, text=True)
        index = json.loads((root / "index.json").read_text()) if result.returncode == 0 else {}
        check("index builder excludes inactive lessons", set(index.get("entries", {})) ==
              {"FM:active", "FM:deprecated-topic"})
        check("inactive tokens cannot produce postings", all(key in {"FM:active", "FM:deprecated-topic"}
              for keys in index.get("postings", {}).values() for key in keys))
        check("index diagnostics distinguish retirement from missing triggers",
              index.get("diagnostics", {}).get("dropped", {}).get("inactive") == 4)

    for name in failures:
        print("FAIL", name)
    print(f"{'FAIL' if failures else 'PASS'} {checks - len(failures)}/{checks} lifecycle checks")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
