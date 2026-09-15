#!/usr/bin/env python3
"""PreCompact witness: record that a compaction happened, and what it cost.

WHY THIS ONLY WATCHES.

A guard that watches context pressure can only be registered on turn-boundary
events -- UserPromptSubmit, Stop. AUTOMATIC compaction happens BETWEEN turns, so
neither fires for it. Such a guard catches the compaction a person was about to
type anyway and is structurally blind to the one nobody chose.

PreCompact is the only event that sees an automatic compaction. It also cannot
fix it: a hook is a shell command, there is no agent turn in flight, and
distillation needs a model. Blocking with exit 2 is worse than useless -- a
blocked compaction on a full context stalls the session with no way forward.

So this does the one useful thing left. It writes down that a compaction
happened and how many catalog entries existed at that moment. SessionStart
compares consecutive records for a session: same entry count across two
compactions means the window collapsed twice and nothing was written down.

That number does not exist anywhere today. Nothing counts how often compaction
eats signal that was never captured, which is why the loop has never been able
to say whether the backstop is load-bearing or decorative.

NEVER blocks, NEVER fails. Every path exits 0. A witness that can break the
thing it observes is not a witness.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE = Path(os.environ.get("RECALL_HOME") or Path.home() / ".recall")
HOST = Path(os.environ.get("RECALL_HOST_DIR") or Path.home() / ".claude")
LOG = Path(os.environ.get("RECALL_COMPACTION_LOG") or STATE / "compaction-log.jsonl")
CATALOGS = Path(os.environ.get("RECALL_CATALOG_DIR") or STATE / "catalogs")
MAX_INPUT = 256 * 1024


def entry_count() -> dict:
    """Count list items -- lines starting `- ` at column 0. Cheap and exact: a
    YAML parse of 2.4 MB costs over a second and this runs while the user waits.

    NOT `- id:`. Entries are written with sorted keys, so most begin
    `- affected_pattern:` and only a handful begin `- id:`. Counting that prefix
    returned 6 and 21 against a real 704 and 691 -- a plausible number, wrong by
    two orders of magnitude, and nothing downstream would have questioned it."""
    out = {}
    for name in ("FAILURE_MODES", "PROCESS_FAILURES", "DECISIONS"):
        f = CATALOGS / f"{name}.yaml"
        try:
            with open(f, "rb") as fh:
                out[name[:2]] = sum(1 for line in fh if line.startswith(b"- "))
        except OSError:
            pass
    return out


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        payload = json.loads(raw.decode("utf-8", "replace")) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    # VERIFIED 2026-09-15, first real firing: stdin DOES carry `trigger`, with
    # the same vocabulary the settings matcher uses. A user-typed /compact wrote
    # {"trigger":"manual"}. So the field is real and the fallback chain below is
    # belt-and-braces, not the expected path. Still unobserved: an `"auto"`
    # record -- automatic compaction has not fired since the witness was
    # installed, so the value that actually matters is the one never yet seen.
    # Keep the "unknown" default: a missing field must not become a false
    # "auto", which would silently inflate exactly the count this exists to make.
    trigger = payload.get("trigger") or payload.get("matcher") or "unknown"
    session = payload.get("session_id") or "unknown"

    record = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_id": session,
        "trigger": trigger,
        "entries": entry_count(),
        "hook_event": payload.get("hook_event_name") or "PreCompact",
    }
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        pass          # a failed write must never cost the user their compaction
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)   # belt and braces: nothing here is worth failing a compaction
