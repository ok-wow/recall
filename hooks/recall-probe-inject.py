#!/usr/bin/env python3
"""Surface catalogued failure modes INTO a live conversation.

The catalogs carry 1,074 `probe_when` triggers -- conditions their authors
wrote to say "show me when this is about to happen". Until this hook, every
consumer of that field was a skill (debugging, PR review, the build loop, the
PR workflow), so an entry only ever fired if the model first remembered to
invoke the right skill -- the same class of miss the entry was written to
prevent. 92% of FAILURE_MODES entries had never recurred once.

Runs on UserPromptSubmit (what is about to be attempted) and on PostToolUse for
Edit/Write (what is actually being changed). Both harnesses use the same event
names and the same stdin/stdout JSON contract, so one core serves either.

Design constraints, in priority order:
  1. Never break a session. Any failure exits 0 silently.
  2. Never be noisy. A channel that fires on everything gets ignored, and then
     the real hit is invisible too. Hence: literal backticked tokens only, an
     idf-weighted score, a specificity floor, at most 2 entries, and once per
     entry per session.
  3. Be measurable. Every surface is logged so `surfaced` can be compared
     against `recurrences` later. Showing an entry is not the same as it
     working, and the corpus has no evidence either way yet.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# Every path below resolves through an environment variable with a default.
# Nothing is written as an absolute path.
HOME = Path(
    os.environ.get("RECALL_HOME") or str(Path.home() / ".recall")
).expanduser()

INDEX_PATH = Path(os.environ.get("RECALL_PROBE_INDEX")
                  or HOME / "probe-index.json").expanduser()
# Per-session dedupe state. Overridable for the same reason SURFACED_LOG is: the
# suite drives this hook as a subprocess, and writing fixture state into the live
# store both pollutes it and lets a test perturb a real session's dedupe.
STATE_DIR = Path(
    os.environ.get("RECALL_PROBE_STATE_DIR") or str(HOME / "probe-state")
).expanduser()
# The signal log is the ONLY evidence of whether retrieval works, so a test run
# must never write to it. test_probe_inject.py drives this hook as a subprocess
# and for months its fixtures landed here under session ids like `regress-<hash>-p10`
# -- 170 of 378 rows, 45% of the corpus of evidence, moving every time the suite
# ran. Overridable so the harness can point somewhere disposable.
SURFACED_LOG = Path(
    os.environ.get("RECALL_PROBE_SURFACED_LOG") or str(HOME / "surfaced.jsonl")
).expanduser()

MAX_ENTRIES = 2
MIN_SCORE = 3.0
# A single matching token must be this long to fire on its own. Measured over
# the first 227 live fires: at 8 this admitted `worktree`, `fallback`,
# `pre-commit`, `services/` and `origin/main` -- ordinary developer vocabulary
# that says nothing about which entry is relevant. 12 keeps the tokens that
# genuinely identify a situation (`allow-same-origin`, `getcomputedstyle`,
# `merge-base --is-ancestor`) and drops the rest. It also matches the index
# builder's own LONE_WORD_SPECIFIC_LEN, which the two had silently diverged on.
LONE_TOKEN_MIN_LEN = 12

# Length alone is the wrong test: `table=true` is 10 characters and genuinely
# identifies a SQLModel situation, while `origin/main` is 11 and identifies
# nothing. What separates them is CODE SYNTAX -- an assignment, a call, an
# interpolation -- which ordinary developer vocabulary does not contain. `/`,
# `-`, `.` and `_` are deliberately NOT here: they are what `origin/main`,
# `pre-commit` and `services/` are made of.
LONE_TOKEN_CODE_PUNCT = frozenset("=(){}<>$@")


def lone_token_qualifies(token: str) -> bool:
    if len(token) >= LONE_TOKEN_MIN_LEN:
        return True
    if any(c in LONE_TOKEN_CODE_PUNCT for c in token):
        return True
    return any(c.isdigit() for c in token)

# The catalog describes the Recall loop itself, so writing ABOUT the loop
# matches entries about the loop. `probe_when` alone produced 22 of the first
# 227 fires -- the single largest source, every one of them self-referential
# noise. A hit carried only by this vocabulary is not evidence of anything.
SELF_REFERENTIAL = frozenset({
    "probe_when", "probe_list.py", "failure_modes.yaml", "process_failures.yaml",
    "learnings.yaml", "artifacts_registry.yaml", "extracted_registry.yaml",
    "compound", "recurrences", "fix_pattern",
})

# A fire is INJECTED whenever it clears the bar above, but only a strong one is
# worth interrupting a person for: two independent specific tokens agreeing,
# and a score well clear of the floor. Roughly one per session at these values.
VISIBLE_MIN_TOKENS = 2
VISIBLE_MIN_SCORE = 10.0
MAX_HAYSTACK = 200_000
MAX_INPUT_BYTES = 4_194_304


def load_index() -> dict | None:
    try:
        with INDEX_PATH.open() as fh:
            return json.load(fh)
    except Exception:
        return None


def haystack_for(payload: dict, event: str) -> str:
    if event == "UserPromptSubmit":
        return str(payload.get("prompt") or "")[:MAX_HAYSTACK]

    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        return ""
    parts = []
    for field in ("file_path", "path", "notebook_path"):
        v = ti.get(field)
        if isinstance(v, str):
            parts.append(v)
    # The NEW text only. Matching old_string would fire on the very code the
    # edit is removing, which is backwards.
    for field in ("content", "new_string", "new_str"):
        v = ti.get(field)
        if isinstance(v, str):
            parts.append(v)
    edits = ti.get("edits")
    if isinstance(edits, list):
        for e in edits[:50]:
            if isinstance(e, dict) and isinstance(e.get("new_string"), str):
                parts.append(e["new_string"])
    return "\n".join(parts)[:MAX_HAYSTACK]


def match(index: dict, haystack: str) -> list[tuple[str, float, list[str]]]:
    hay = haystack.lower()
    if not hay.strip():
        return []
    # Specificity is decided at build time by token SHAPE (punctuation,
    # camelCase, digits, or real length). Length alone let `completed` and
    # `background` carry a hit against a system notification on the very first
    # live fire.
    specific_tokens = set(index.get("specific_tokens", ()))
    postings = index["postings"]
    weights = index["weights"]

    scores: dict[str, float] = {}
    hits: dict[str, list[str]] = {}
    specific: set[str] = set()

    for token, keys in postings.items():
        if token not in hay:
            continue
        w = weights.get(token, 1.0)
        is_specific = token in specific_tokens
        for key in keys:
            scores[key] = scores.get(key, 0.0) + w
            hits.setdefault(key, []).append(token)
            if is_specific:
                specific.add(key)

    # idf is computed over 677 catalog entries, so a token appearing in ONE
    # entry gets the maximum weight -- even when that token is `/tmp/`, which
    # occurs in roughly every developer context alive. Rarity in the catalog is
    # not rarity in the world, and no weighting over this corpus can know the
    # difference. So a LONE token has to carry its own distinctiveness:
    # `allow-same-origin` can, `/tmp/` cannot. Two independent tokens agreeing
    # is the other way to earn a hit.
    ranked = []
    for key, score in scores.items():
        if key not in specific or score < MIN_SCORE:
            continue
        specific_hits = [t for t in hits[key] if t in specific_tokens]
        if all(t in SELF_REFERENTIAL for t in specific_hits):
            continue
        if len(specific_hits) < 2 and not any(
            lone_token_qualifies(t) for t in specific_hits
        ):
            continue
        ranked.append((key, score, hits[key]))
    ranked.sort(key=lambda r: (-r[1], r[0]))
    return ranked[:MAX_ENTRIES]


def already_shown(session_id: str, keys: list[str]) -> set[str]:
    if not session_id:
        return set()
    f = STATE_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', session_id)[:128]}.json"
    try:
        seen = set(json.loads(f.read_text()))
    except Exception:
        seen = set()
    fresh = [k for k in keys if k not in seen]
    if fresh:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(sorted(seen | set(fresh))))
        except Exception:
            pass
    return seen


def log_surfaced(session_id: str, event: str, rows: list[tuple[str, float, list[str]]]) -> None:
    try:
        SURFACED_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SURFACED_LOG.open("a") as fh:
            for key, score, toks in rows:
                fh.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "session": session_id,
                    "event": event,
                    "entry": key,
                    "score": round(score, 2),
                    "tokens": toks[:6],
                }, separators=(",", ":")) + "\n")
    except Exception:
        pass


def render(index: dict, rows: list[tuple[str, float, list[str]]]) -> str:
    lines = [
        "A catalogued failure mode matches what you are about to do. This is a",
        "prior observation, not an instruction — check whether it actually applies.",
        "",
    ]
    for key, _score, toks in rows:
        e = index["entries"][key]
        rec = e.get("recurrences", 0)
        seen = f", seen {rec}x before" if isinstance(rec, int) and rec > 0 else ""
        lines.append(f"[{e['catalog']}] {e['id']}{seen}")
        lines.append(f"  matched: {', '.join(repr(t) for t in toks[:4])}")
        if e.get("summary"):
            lines.append(f"  what happened: {e['summary'][:320]}")
        if e.get("fix"):
            lines.append(f"  what to do:    {e['fix'][:320]}")
        lines.append("")
    return "\n".join(lines).rstrip()


def visible_notice(index: dict, rows: list[tuple[str, float, list[str]]]) -> str | None:
    """The one-line receipt a PERSON sees when the catalog actually caught something.

    Injection is machine-facing by design, which means every hit has so far been
    invisible: the loop could be working perfectly and still feel like it does
    nothing. Capture says "this is worth writing down"; this notice is the
    other half -- "we already wrote it down." Strong hits only; a noisy
    receipt is worse than
    a silent one, because it teaches the reader to ignore it.
    """
    specific_tokens = set(index.get("specific_tokens") or [])
    for key, score, toks in rows:
        distinct = {t for t in toks if t in specific_tokens and t not in SELF_REFERENTIAL}
        if score < VISIBLE_MIN_SCORE or len(distinct) < VISIBLE_MIN_TOKENS:
            continue
        e = index["entries"][key]
        rec = e.get("recurrences", 0)
        again = " and it has bitten us more than once" if isinstance(rec, int) and rec > 0 else ""
        return (
            f"Compounding: we hit this before{again} — {e['id']}. "
            f"Pulled up what we learned."
        )
    return None


def run() -> None:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        return
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return

    event = payload.get("hook_event_name")
    if event not in ("UserPromptSubmit", "PostToolUse"):
        return
    if event == "PostToolUse" and payload.get("tool_name") not in (
        "Edit", "Write", "NotebookEdit", "MultiEdit", "apply_patch"
    ):
        return

    index = load_index()
    if not index:
        return

    rows = match(index, haystack_for(payload, event))
    if not rows:
        return

    session_id = str(payload.get("session_id") or "")
    seen = already_shown(session_id, [r[0] for r in rows])
    rows = [r for r in rows if r[0] not in seen]
    if not rows:
        return

    log_surfaced(session_id, event, rows)
    out = {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": render(index, rows),
        }
    }
    notice = visible_notice(index, rows)
    if notice:
        out["systemMessage"] = notice
    print(json.dumps(out, separators=(",", ":")))


def main() -> int:
    try:
        run()
    except Exception:
        pass          # a knowledge hook must never be the reason a turn fails
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
