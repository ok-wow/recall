#!/usr/bin/env python3
"""Deterministic transcript digest for Recall drains.

Encodes learnings.yaml::preprocess-oversized-transcripts-into-digests-before-
parallel-distill (2026-07-11): oversized .jsonl transcripts are dominated by
tool payloads; a distill agent needs user text verbatim, truncated assistant
text, tool NAMES + errors only, and nothing else.

Two changes over v1 (2026-09-12), both driven by measurement on the 15 parked
oversized sessions:

1. Codex transcripts produced an EMPTY digest. v1 knew only the Claude Code
   schema ({"type":"user"|"assistant"}); Codex writes
   {"type":"response_item","payload":{"type":"message"|"function_call"|...}}.
   One of the 15 was Codex, and an empty digest is worse than no digest: it
   reads as "session had nothing in it". Both schemas are normalised to one
   event stream here.

2. v1 truncated at the HEAD. It emitted in file order and stopped at the cap,
   so a session whose digest demand exceeds the budget kept only its opening.
   Measured on the 390 MB session: demand 1619 KB against a 320 KB cap, of
   which assistant narration is 87.6% (4352 blocks, avg 333 chars) -- so ~80%
   of the session was dropped, INCLUDING the end. For compounding that is the
   worst possible slice to lose: what was fixed, what was verified, and what
   was concluded all cluster in the final turns. v2 fits the whole session
   instead -- every user turn and every tool error is kept, the tail is kept
   whole, and earlier assistant narration is uniformly sampled to fit.

Caps: user 6000 chars, assistant 1800, error 400, total digest 320KB
(RECALL_DIGEST_TOTAL_CAP overrides). Attachments, file-read payloads, stdout,
thinking/reasoning, and hook noise are dropped. Stdlib only.

Usage: digest_transcript.py <transcript.jsonl> <out.md> [assistant_cap]
Exit 0 on a usable digest, 2 if the transcript yielded too little to be worth
distilling (the caller should park it rather than hand a worker an empty file).
"""
import json
import os
import sys

USER_CAP = 6000
ASSISTANT_CAP = 1800
ERROR_CAP = 400
TOTAL_CAP = int(os.environ.get("RECALL_DIGEST_TOTAL_CAP")
                or os.environ.get("OKWOW_DIGEST_TOTAL_CAP") or 320 * 1024)

# Fraction of the session, measured from the end, whose assistant narration is
# kept whole no matter what. The conclusion of a session is its highest-value
# region and must never be the part that sampling drops.
TAIL_FRACTION = 0.15

# Event kinds that may be sampled away under budget pressure, in contrast to
# `user` and `error`, which are always kept whole.
SAMPLEABLE = ("assistant", "tools", "meta")

# Below this a digest is not worth a worker's run. Signals "park me" (exit 2)
# rather than producing a file that reads as an empty session.
MIN_USEFUL_BYTES = 2048

# Substrings that mark hook/system noise inside user-role text blocks.
NOISE_MARKERS = (
    "<system-reminder>",
    "COMPOUND CHECK",
    "CHAPTER BOUNDARY EVALUATION",
    "THREE-AGENT-LOOP ELIGIBILITY CHECK",
    "<command-name>",
    "Caveat: The messages below were generated",
)

# Codex injects its whole operating context as `developer`-role messages. They
# are harness furniture, identical across sessions, and carry no session signal.
CODEX_DROP_ROLES = ("developer", "system")


def clip(text, cap):
    text = text.strip()
    if len(text) <= cap:
        return text
    return text[:cap] + f" …[+{len(text) - cap} chars]"


def user_cap_for(text, user_cap=USER_CAP):
    """Harness-injected user-role payloads get a stub budget, not the full cap."""
    if "<task-notification>" in text or "<local-command-stdout>" in text:
        return min(300, user_cap)
    if text.startswith("Base directory for this skill:") or "<ci-monitor-event>" in text:
        return min(200, user_cap)
    if "continued from a previous conversation" in text:
        return min(2000, user_cap)
    return user_cap


def text_blocks(content):
    """Yield (kind, text) from a Claude Code message content list or string."""
    if isinstance(content, str):
        yield ("text", content)
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            yield ("text", block.get("text", ""))
        elif btype == "tool_use":
            yield ("tool_use", block.get("name", "?"))
        elif btype == "tool_result":
            # Keep errors only; drop successful payloads (file reads, stdout).
            if block.get("is_error"):
                inner = block.get("content")
                if isinstance(inner, list):
                    err = " ".join(
                        b.get("text", "") for b in inner if isinstance(b, dict)
                    )
                else:
                    err = str(inner)
                yield ("error", err)


def codex_text(content):
    """Codex message content: a list of {type: input_text|output_text, text}."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in (
            "input_text",
            "output_text",
            "text",
        ):
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def extract(rec, assistant_cap, user_cap=USER_CAP):
    """Normalise one record from either schema into (kind, text) events.

    kind is one of: meta, user, assistant, tools, error. Only `assistant` is
    ever sampled away; everything else is structural and always kept.
    """
    rtype = rec.get("type")

    # --- Claude Code schema -------------------------------------------------
    if rtype == "custom-title":
        yield ("meta", f"\n## [title] {rec.get('customTitle', '')}")
    elif rtype == "pr-link":
        yield (
            "meta",
            f"[pr-link] {rec.get('prRepository')}#{rec.get('prNumber')} {rec.get('prUrl', '')}",
        )
    elif rtype == "summary":
        yield ("meta", f"[summary] {clip(str(rec.get('summary', '')), ASSISTANT_CAP)}")
    elif rtype == "user":
        for kind, text in text_blocks(rec.get("message", {}).get("content", "")):
            if kind == "text":
                if any(m in text for m in NOISE_MARKERS) or not text.strip():
                    continue
                yield ("user", f"\nUSER: {clip(text, user_cap_for(text, user_cap))}")
            elif kind == "error" and text.strip():
                yield ("error", f"  [tool-error] {clip(text, ERROR_CAP)}")
    elif rtype == "assistant":
        tools = []
        for kind, text in text_blocks(rec.get("message", {}).get("content", [])):
            if kind == "text" and text.strip():
                yield ("assistant", f"ASSISTANT: {clip(text, assistant_cap)}")
            elif kind == "tool_use":
                tools.append(text)
        if tools:
            yield ("tools", f"  [tools: {', '.join(tools)}]")

    # --- Codex schema -------------------------------------------------------
    elif rtype == "response_item":
        payload = rec.get("payload") or {}
        ptype = payload.get("type")
        if ptype in ("message", "agent_message"):
            role = payload.get("role", "assistant")
            if role in CODEX_DROP_ROLES:
                return
            text = codex_text(payload.get("content", ""))
            if not text.strip() or any(m in text for m in NOISE_MARKERS):
                return
            if role == "user":
                yield ("user", f"\nUSER: {clip(text, user_cap_for(text, user_cap))}")
            else:
                yield ("assistant", f"ASSISTANT: {clip(text, assistant_cap)}")
        elif ptype in ("function_call", "custom_tool_call", "local_shell_call"):
            yield ("tools", f"  [tools: {payload.get('name', '?')}]")
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            # Same rule as Claude Code tool_result: errors only, payloads dropped.
            out = payload.get("output")
            if isinstance(out, str):
                try:
                    out = json.loads(out)
                except (json.JSONDecodeError, ValueError):
                    out = {}
            if isinstance(out, dict):
                failed = out.get("exit_code") not in (0, None) or out.get("is_error")
                if failed:
                    body = str(out.get("output") or out.get("content") or "")
                    if body.strip():
                        yield ("error", f"  [tool-error] {clip(body, ERROR_CAP)}")
    elif rtype == "session_meta":
        payload = rec.get("payload") or {}
        inst = payload.get("instructions") or ""
        if inst:
            yield ("meta", f"\n## [codex session] {clip(str(inst), 200)}")


def collect(path, assistant_cap, user_cap=USER_CAP):
    """Stream the transcript into an event list, removing duplication at source.

    Two record classes are emitted per-record by the harness rather than per-
    event, so a long session repeats them thousands of times: `pr-link` and
    titles (measured 6189 meta events in one 390 MB session, nearly all the
    same pr-link) and tool lines (15222, avg 25 chars). Deduplicating them here
    is strictly better than sampling them away later -- a repeat carries no
    information the first copy did not.
    """
    events = []
    seen_meta = set()
    for raw in open(path, errors="replace"):
        try:
            rec = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        for kind, text in extract(rec, assistant_cap, user_cap):
            if kind == "meta":
                if text in seen_meta:
                    continue
                seen_meta.add(text)
            elif kind == "tools" and events and events[-1][0] == "tools":
                prev = events[-1][1]
                base, _, count = prev.rpartition(" x")
                if count.isdigit() and base == text:
                    events[-1] = (kind, f"{text} x{int(count) + 1}")
                    continue
                if prev == text:
                    events[-1] = (kind, f"{text} x2")
                    continue
            events.append((kind, text))
    return events


def _select(idx, sizes, budget, n_events):
    """Choose as many of `idx` as fit `budget`, tail first then uniformly.

    Order matters more than volume for compounding: the end of a session holds
    what was fixed and what was verified, so the final TAIL_FRACTION is taken
    before anything else, and the remainder is sampled at a uniform stride
    across the earlier session rather than taken in file order. Taking them in
    order is what made v1 keep only a session's opening.
    """
    tail_start = n_events - int(n_events * TAIL_FRACTION)
    tail = [i for i in idx if i >= tail_start]
    head = [i for i in idx if i < tail_start]

    chosen, running = [], 0
    for i in tail:
        if running + sizes[i] > budget:
            break
        chosen.append(i)
        running += sizes[i]

    remaining = budget - running
    if head and remaining > 0:
        avg = max(1, sum(sizes[i] for i in head) // len(head))
        room = max(0, remaining // avg)
        if room >= len(head):
            candidates = head
        elif room > 0:
            stride = len(head) / room
            candidates = sorted(
                {head[min(len(head) - 1, int(k * stride))] for k in range(room)}
            )
        else:
            candidates = []
        for i in candidates:
            if running + sizes[i] > budget:
                break
            chosen.append(i)
            running += sizes[i]
    return chosen


def fit(events):
    """Select which events to emit so the whole session fits the byte budget.

    Two tiers. The user's own turns and every tool error are what the session
    was about and what actually went wrong, so they are filled first. Assistant
    narration, tool names and titles compete for whatever budget is left. Both
    tiers are selected tail-first, never in file order, so a session that
    overflows loses breadth rather than losing its ending.
    """
    sizes = [len(text) + 1 for _, text in events]
    total = sum(sizes)
    if total <= TOTAL_CAP:
        return set(range(len(events))), 0

    n = len(events)
    sampleable = [i for i, (kind, _) in enumerate(events) if kind in SAMPLEABLE]
    inviolable = [i for i in range(n) if events[i][0] not in SAMPLEABLE]
    fixed = sum(sizes[i] for i in inviolable)

    if fixed >= TOTAL_CAP:
        # Even the user's own turns overflow. Sample THEM tail-first as well --
        # keeping the first 92% and dropping the last 8% is the worst trade
        # available, and it is exactly what in-order truncation does.
        kept = set(_select(inviolable, sizes, TOTAL_CAP, n))
        return kept, n - len(kept)

    keep = set(inviolable)
    keep |= set(_select(sampleable, sizes, TOTAL_CAP - fixed, n))
    return keep, n - len(keep)


def main(path, out_path, assistant_cap=ASSISTANT_CAP):
    # Refuse to write over the input. The drain's retry path once handed this
    # the SAME path for both: a marker already digested has transcript_path
    # pointing AT the digest, so opening the output truncated it to zero before
    # a single line was read, destroying the session's only reduced copy.
    # Measured 2026-09-13: 108689 bytes -> 0. The drain is fixed too; this is the
    # backstop, because the reducer is the thing that does the damage.
    try:
        if os.path.realpath(path) == os.path.realpath(out_path):
            sys.stderr.write("digest: input and output are the same file\n")
            return 1
    except OSError:
        pass
    events = collect(path, assistant_cap)

    # When the user's own turns alone overflow the budget, clipping each turn
    # shorter keeps ALL of them; truncating in order would keep the early ones
    # whole and silently drop the end of the session -- the same head-bias this
    # rewrite exists to remove. Measured on a 285 MB session: 1373 user turns,
    # 347 KB against a 320 KB cap.
    inviolable = sum(len(t) + 1 for k, t in events if k not in SAMPLEABLE)
    if inviolable > TOTAL_CAP:
        shrunk = max(400, int(USER_CAP * (TOTAL_CAP / inviolable) * 0.9))
        events = collect(path, assistant_cap, user_cap=shrunk)

    keep, elided = fit(events)
    body = [text for i, (_, text) in enumerate(events) if i in keep]

    header = []
    if elided:
        header.append(
            f"[DIGEST: {elided} events sampled out to fit the "
            f"{TOTAL_CAP // 1024}KB budget. Every user turn and tool error is "
            f"present; coverage spans the whole session, and the final "
            f"{int(TAIL_FRACTION * 100)}% is kept whole.]"
        )
    out = "\n".join(header + body)

    with open(out_path, "w") as f:
        f.write(out)

    size = len(out)
    print(f"digest: {size} bytes, {len(body)} lines, {elided} elided -> {out_path}")
    return 0 if size >= MIN_USEFUL_BYTES else 2


if __name__ == "__main__":
    cap = int(sys.argv[3]) if len(sys.argv) > 3 else ASSISTANT_CAP
    sys.exit(main(sys.argv[1], sys.argv[2], cap))
