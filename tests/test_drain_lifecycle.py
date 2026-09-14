#!/usr/bin/env python3
"""Marker-lifecycle fixtures for the drain.

Runs the REAL drain script against a stub worker inside a throwaway
COMPOUND_HOME, so none of this touches the live queue.

The case that matters is `completed_no_clear`: a worker that exits 0 and leaves
the marker alone. For three months that read as total failure and burned retry
attempts toward quarantine, because progress was measured by whether the marker
disappeared -- a side effect the worker was asked, in prose, to perform.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK_DIR = Path(os.environ.get("COMPOUND_HOOK_DIR", REPO / "hooks"))
DRAIN = HOOK_DIR / "compound-drain.sh"
SESSION = "11111111-2222-3333-4444-555555555555"


def run_case(stub_body: str) -> dict:
    home = Path(tempfile.mkdtemp(prefix="draintest-"))
    (home / "pending").mkdir(parents=True)
    (home / "host").mkdir()

    (home / "pending" / f"{SESSION}.json").write_text(
        json.dumps({"session_id": SESSION,
                    "transcript_path": str(home / "t.jsonl"),
                    "transcript_size_bytes": 120_000})
    )
    (home / "t.jsonl").write_text("{}\n")

    stub = home / "agent-stub"
    stub.write_text("#!/bin/sh\n" + stub_body + "\n")
    stub.chmod(0o755)

    env = dict(os.environ)
    env.update({
        # HOME is redirected too, so anything still home-relative cannot reach
        # the real one.
        "HOME": str(home),
        "COMPOUND_HOME": str(home),
        "COMPOUND_HOST_DIR": str(home / "host"),
        "COMPOUND_AGENT_BIN": str(stub),
        "COMPOUND_DRAIN_BUDGET": "60",
        "COMPOUND_DRAIN_SESSION_TIMEOUT": "8",
        "COMPOUND_DRAIN_PERMISSIONS": str(home / "perms.json"),
    })
    env.pop("COMPOUND_WORKER", None)
    # The drain FAILS CLOSED without deny rules: it will not run an unattended
    # agent in auto mode with nothing denied. A fixture handing it an empty
    # deny list therefore exercises the refusal path, not the drain. Give it a
    # real rule so the cases below test what they claim to test.
    (home / "perms.json").write_text(
        '{"permissions":{"deny":["Bash(rm -rf:*)","Bash(git push --force:*)"]}}')

    subprocess.run(["bash", str(DRAIN)], env=env, capture_output=True, text=True, timeout=120)

    return {
        "marker": (home / "pending" / f"{SESSION}.json").exists(),
        "ledger": (home / "processed" / f"{SESSION}.json").exists(),
        "attempt": (home / "drain-attempts" / SESSION).exists(),
        "authdown": (home / "auth-down.json").exists(),
        "home": home,
    }


CASES = [
    # name, stub, expected {marker kept?, ledger?, attempt burned?, authdown?}
    ("worker completed, left the marker (the bug)",
     'echo "0 new artifacts, 0 routed - DISCARDED"; echo "Marker: requires explicit approval"; exit 0',
     {"marker": False, "ledger": True, "attempt": False, "authdown": False}),

    ("worker completed and cleared its own marker",
     f'rm -f "$COMPOUND_HOME/pending/{SESSION}.json"; echo done; exit 0',
     {"marker": False, "ledger": True, "attempt": False, "authdown": False}),

    ("worker crashed (non-zero exit)",
     'echo "boom" >&2; exit 3',
     {"marker": True, "ledger": False, "attempt": True, "authdown": False}),

    ("infrastructure down",
     'echo "Failed to authenticate. API Error: 401 OAuth access token is invalid."; exit 1',
     {"marker": True, "ledger": False, "attempt": False, "authdown": True}),
]



# --- digest reaping ------------------------------------------------------
# The invariant worth a permanent test is not "old files get deleted", it is
# WHERE the call sits. The drain exits at `[ "$PENDING" -eq 0 ] && exit 0`, and
# an empty queue is the steady state, so a reaper below that guard would never
# run on an idle system -- the exact condition it exists to clean up after.
# `empty queue` below therefore runs the REAL drain with nothing pending: a
# deletion can only happen if reap_digests is above the exit. It fails the day
# someone moves it down.
DAY = 86_400


def run_reap_case(digests: dict, pending=None) -> dict:
    """digests: {name: age_days}. pending: None | "pending" | "quarantined"."""
    home = Path(tempfile.mkdtemp(prefix="reaptest-"))
    (home / "pending").mkdir(parents=True)
    ddir = home / "digests"
    ddir.mkdir(parents=True)
    (home / "host").mkdir()

    now = time.time()
    for name, age in digests.items():
        f = ddir / f"{name}.md"
        f.write_text("digest body\n")
        os.utime(f, (now - age * DAY, now - age * DAY))

    stub = home / "agent-stub"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    marker = json.dumps({"session_id": SESSION,
                         "transcript_path": str(home / "digests" / f"{SESSION}.md"),
                         "transcript_size_bytes": 120_000})
    if pending == "pending":
        (home / "pending" / f"{SESSION}.json").write_text(marker)
    elif pending == "quarantined":
        # digest_marker() repoints transcript_path at the digest BEFORE the worker
        # runs, so a marker that later fails into quarantine still names it.
        q = home / "quarantine/_oversized"
        q.mkdir(parents=True, exist_ok=True)
        (q / f"{SESSION}.json").write_text(marker)

    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "COMPOUND_HOME": str(home),
        "COMPOUND_HOST_DIR": str(home / "host"),
        "COMPOUND_AGENT_BIN": str(stub),
        "COMPOUND_DRAIN_BUDGET": "30",
        "COMPOUND_DRAIN_SESSION_TIMEOUT": "8",
        "COMPOUND_DRAIN_PERMISSIONS": str(home / "perms.json"),
        "COMPOUND_DIGEST_RETENTION_DAYS": "14",
    })
    env.pop("COMPOUND_WORKER", None)
    # The drain FAILS CLOSED without deny rules: it will not run an unattended
    # agent in auto mode with nothing denied. A fixture handing it an empty
    # deny list therefore exercises the refusal path, not the drain. Give it a
    # real rule so the cases below test what they claim to test.
    (home / "perms.json").write_text(
        '{"permissions":{"deny":["Bash(rm -rf:*)","Bash(git push --force:*)"]}}')

    subprocess.run(["bash", str(DRAIN)], env=env, capture_output=True, text=True, timeout=120)
    return {name: (ddir / f"{name}.md").exists() for name in digests}


REAP_CASES = [
    # name, digests {id: age_days}, pending_marker?, expected survival
    ("empty queue still reaps (call site is above the early exit)",
     {"aaaaaaaa-0000-0000-0000-000000000001": 40}, None,
     {"aaaaaaaa-0000-0000-0000-000000000001": False}),

    ("fresh digest is kept",
     {"aaaaaaaa-0000-0000-0000-000000000002": 3}, None,
     {"aaaaaaaa-0000-0000-0000-000000000002": True}),

    ("old digest a queued marker still names is KEPT (data-loss guard)",
     {SESSION: 40}, "pending",
     {SESSION: True}),

    ("old digest a QUARANTINED marker still names is KEPT",
     {SESSION: 40}, "quarantined",
     {SESSION: True}),

    ("mixed: old reaped, fresh kept, in-run",
     {"aaaaaaaa-0000-0000-0000-000000000003": 40,
      "aaaaaaaa-0000-0000-0000-000000000004": 1}, None,
     {"aaaaaaaa-0000-0000-0000-000000000003": False,
      "aaaaaaaa-0000-0000-0000-000000000004": True}),
]


def main() -> int:
    failures = []
    for name, stub, want in CASES:
        got = run_case(stub)
        for key, expected in want.items():
            if got[key] != expected:
                failures.append(
                    f"{name}: {key} expected {expected}, got {got[key]}")
        label = "ok " if all(got[k] == v for k, v in want.items()) else "FAIL"
        print(f"  [{label}] {name}")
        print(f"         marker_kept={got['marker']} ledger={got['ledger']} "
              f"attempt_burned={got['attempt']} infra_marker={got['authdown']}")

    for name, digests, pending, want in REAP_CASES:
        got = run_reap_case(digests, pending)
        ok = got == want
        if not ok:
            failures.append(f"{name}: expected {want}, got {got}")
        print(f"  [{'ok ' if ok else 'FAIL'}] {name}")
        print(f"         survived={got}")

    total = len(CASES) + len(REAP_CASES)
    if failures:
        print(f"\nFAIL {len(failures)} assertion(s)")
        for f in failures:
            print("  -", f)
        return 1
    print(f"\nPASS {total}/{total} lifecycle cases "
          f"({len(CASES)} marker, {len(REAP_CASES)} reap)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
