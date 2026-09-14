#!/usr/bin/env python3
"""Decide whether a transcript contains enough work to be worth distilling.

Replaces the 80 KB byte threshold in the SessionEnd hook. Bytes measured the
wrong thing: a desktop auth-keepalive probe whose entire conversation is
"reply with the single word: ok" weighs 87 KB, because the harness injects
skill, agent and MCP listings as `attachment` records before the first turn.
Seven such probes reached the drain and were distilled into nothing. Meanwhile
a genuinely useful 60 KB session was never queued at all.

Measured over a 55-session queue the split is not marginal:

    probes      1 user turn,    0 tool calls,  15-19 lines
    real work   631+ user turns, 599+ tool calls, 2000+ lines

Nothing sits between those. So this counts turns and tool calls instead, in
both the Claude Code and Codex transcript schemas.

Exit 0 = substantive (enqueue it), exit 1 = not (skip it).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# From the distribution above, any bar between 2 and ~500 separates the two
# populations identically. Set low: the cost of queueing one thin session is a
# cheap drain run, the cost of dropping a real one is signal lost for good.
MIN_USER_TURNS = 3
MIN_TOOL_CALLS = 3

# A SessionEnd hook gets ~1-3 seconds. The largest queued transcript is 149 MB,
# so full parsing is out. Scanning stops the moment substance is proven, which
# for real work is within the first few hundred lines.
MAX_LINES = 20_000
MAX_BYTES = 8 * 1024 * 1024

CLAUDE_TOOL_TYPES = {"tool_use"}
CODEX_TOOL_TYPES = {"function_call", "local_shell_call", "custom_tool_call"}


def classify(path: Path) -> dict:
    user = assistant = tools = lines = 0
    parsed = 0
    scanned = 0
    capped = False

    with path.open(errors="replace") as fh:
        for raw in fh:
            lines += 1
            scanned += len(raw)
            if lines > MAX_LINES or scanned > MAX_BYTES:
                capped = True
                break
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            parsed += 1

            # --- Claude Code: {"type":"user"|"assistant","message":{...}}
            msg = rec.get("message")
            if isinstance(msg, dict):
                role = msg.get("role")
                if role == "user":
                    user += 1
                elif role == "assistant":
                    assistant += 1
                content = msg.get("content")
                if isinstance(content, list):
                    tools += sum(
                        1 for b in content
                        if isinstance(b, dict) and b.get("type") in CLAUDE_TOOL_TYPES
                    )

            # --- Codex: {"type":"response_item","payload":{"type":...}}
            elif rec.get("type") == "response_item":
                payload = rec.get("payload")
                if isinstance(payload, dict):
                    ptype = payload.get("type")
                    if ptype in CODEX_TOOL_TYPES:
                        tools += 1
                    elif ptype == "message":
                        if payload.get("role") == "user":
                            user += 1
                        elif payload.get("role") == "assistant":
                            assistant += 1

            if user >= MIN_USER_TURNS and tools >= MIN_TOOL_CALLS:
                break   # proven; stop reading

    substantive = user >= MIN_USER_TURNS or tools >= MIN_TOOL_CALLS

    # Everything below is the same judgement: only a CONFIDENT no is a no.
    #
    # Hitting the cap means we stopped looking before the end.
    if capped and not substantive:
        substantive = True
    # Lines present but not one of them parsed means the format is not one we
    # read -- a future schema, a corrupted write, a partially flushed file.
    # That is unreadable, which is not the same as empty, and the first draft
    # of this function conflated them and would have dropped the session.
    if lines > 0 and parsed == 0:
        substantive = True

    return {
        "user_turns": user,
        "assistant_turns": assistant,
        "tool_calls": tools,
        "lines_scanned": lines,
        "records_parsed": parsed,
        "capped": capped,
        "substantive": substantive,
    }


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: transcript_substance.py <transcript>"}))
        return 0     # unknown -> enqueue, same fail-open posture
    path = Path(sys.argv[1])
    if not path.is_file():
        print(json.dumps({"error": "not a file", "substantive": True}))
        return 0
    try:
        result = classify(path)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}", "substantive": True}))
        return 0
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["substantive"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
