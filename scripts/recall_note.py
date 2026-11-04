#!/usr/bin/env python3
"""Record one model-judged note in the inbox and print a one-line receipt.

    recall_note.py KIND "one line" [--evidence POINTER] [--confidence candidate|verified|stated]
                                   [--session ID]

KIND is failure, process_failure, decision, success, workflow, guardrail or
policy. Confidence is candidate (the default: judged, not checked), verified
(a fix that ran) or stated (a person said it). The text is scrubbed before it is
written; if scrubbing is uncertain, nothing is written.

The receipt says "queued" once the line is in $RECALL_HOME/inbox/events.jsonl,
"synced" once it also reached the inbox/cloud branch, and "NOT queued" (exit 1
or 2) when it did not happen. Run it when a fix was verified, a decision with a
rejected alternative was made, or the user corrected the same thing twice.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import friction_events as fe  # noqa: E402
import scrub  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("kind")
    ap.add_argument("text")
    ap.add_argument("--evidence", default="")
    ap.add_argument("--confidence", default="candidate")
    ap.add_argument("--session", default=os.environ.get("RECALL_SESSION_ID", "unknown"))
    a = ap.parse_args(argv)

    home = fe.recall_home()
    text, why = scrub.scrub_checked(" ".join(a.text.split()))
    ev = {
        "type": "note", "kind": a.kind, "text": text or "", "confidence": a.confidence,
        "signature": "note:%s:%s" % (a.kind, re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60]),
        "count": 1, "session_id": a.session, "host": fe.host_name(),
        "evidence": {"pointer": a.evidence} if a.evidence else {}, "ts": fe.now_iso(),
    }
    errs = [why] if why else fe.validate_event(ev)
    if errs:
        print("recall: note NOT queued: " + "; ".join(errs))
        return 2
    ok, err = fe.append_events(home, [ev])
    if not ok:
        print(f"recall: note NOT queued: {err}")
        return 1
    label, tail = "queued", ""
    if fe.sync_enabled():
        synced, reason = fe.light_sync(home, a.session, "")()
        if synced:
            label = "synced"
        else:
            tail = f" (not synced: {' '.join(str(reason).split())[:80]})"
    print(f"recall: {label} note ({a.kind}, {a.confidence}): {ev['text'][:80]}{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
