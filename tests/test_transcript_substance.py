#!/usr/bin/env python3
"""Fixtures for the SessionEnd substance gate.

Run after any change to transcript_substance.py or the SessionEnd hook:
    python3 tests/test_transcript_substance.py

The synthetic fixtures encode the shapes that actually mattered. The probe is
the real one: ~87 KB of injected `attachment` records wrapped around a single
"reply with the single word: ok" turn. It cleared the old 80 KB threshold seven
times. Both transcript schemas are covered, because a Claude-only parser reads
every Codex session as 0 turns / 0 tools and would have silently dropped all of
them.

Paths resolve through the environment, never absolutely:
    COMPOUND_SKILL_DIR    the scripts directory      (default: <repo>/scripts)
    COMPOUND_FIXTURE_DIR  the fixtures directory     (default: <repo>/tests/fixtures)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_DIR = TESTS_DIR.parent

SKILL_DIR = Path(os.environ.get("COMPOUND_SKILL_DIR") or REPO_DIR / "scripts").expanduser()
FIXTURE_DIR = Path(os.environ.get("COMPOUND_FIXTURE_DIR") or TESTS_DIR / "fixtures").expanduser()

GATE = SKILL_DIR / "transcript_substance.py"
CORPUS = FIXTURE_DIR / "no_substance_corpus"


def write(lines: list[dict]) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for l in lines:
        f.write(json.dumps(l) + "\n")
    f.close()
    return Path(f.name)


def substantive(path: Path) -> bool:
    return subprocess.run(
        [sys.executable, str(GATE), str(path)], capture_output=True, text=True
    ).returncode == 0


def claude_probe() -> list[dict]:
    # 10 attachment records is what inflates a 2-message session past 80 KB.
    pad = "x" * 8000
    out: list[dict] = [{"type": "attachment", "content": pad} for _ in range(10)]
    out.append({"type": "user", "message": {"role": "user", "content": "reply with the single word: ok"}})
    out.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}})
    return out


def claude_real() -> list[dict]:
    out: list[dict] = []
    for i in range(4):
        out.append({"type": "user", "message": {"role": "user", "content": f"do thing {i}"}})
        out.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {}},
        ]}})
    return out


def codex_probe() -> list[dict]:
    return [
        {"type": "session_meta", "payload": {}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "ok?"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "ok"}},
    ]


def codex_real() -> list[dict]:
    out: list[dict] = [{"type": "session_meta", "payload": {}}]
    for i in range(4):
        out.append({"type": "response_item", "payload": {"type": "message", "role": "user", "content": f"q{i}"}})
        out.append({"type": "response_item", "payload": {"type": "local_shell_call", "call_id": str(i)}})
    return out



def real_corpus() -> tuple[int, int, list[str]]:
    """13 real sessions the gate parked as non-substantive on 2026-09-10.

    Reduced to STRUCTURE ONLY -- role, record type, and content-block types, no
    text -- because that is the entire set of fields transcript_substance.py
    reads. Verified equivalent when captured: all 13 produced byte-identical
    counts and exit codes before and after stripping. 1614 KB -> 52 KB, and no
    conversation content enters the repo.

    Kept as fixtures rather than as pointers into $COMPOUND_HOST_DIR/projects
    because transcripts age off that disk -- the sibling `_undrainable` tier is
    21 markers whose transcripts expired before anything read them. A corpus
    that lives only as a path is a corpus that will quietly become empty.

    Every one of these is 1-2 user turns and ZERO tool calls, yet 95-140 KB on
    disk. That gap is the whole reason the gate counts turns instead of bytes.
    """
    if not CORPUS.is_dir():
        return 0, 0, ["corpus directory missing: " + str(CORPUS)]
    files = sorted(CORPUS.glob("*.jsonl"))
    if not files:
        return 0, 0, ["corpus is empty: " + str(CORPUS)]
    bad = [f.name[:8] for f in files if substantive(f)]
    errs = [f"real corpus: {n} now reads as SUBSTANTIVE -- the gate got looser" for n in bad]
    return len(files) - len(bad), len(files), errs


def main() -> int:
    cases = [
        (False, "claude probe (1 turn, 0 tools, 80KB of attachments)", claude_probe()),
        (True, "claude real (4 turns, 4 tool calls)", claude_real()),
        (False, "codex probe (1 turn, 0 tools)", codex_probe()),
        (True, "codex real (4 turns, 4 shell calls)", codex_real()),
        (True, "tools only, one prompt (agentic single-shot)", [
            {"type": "user", "message": {"role": "user", "content": "fix it"}},
        ] + [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Edit", "input": {}}]}} for _ in range(5)
        ]),
    ]

    failures = []
    paths = []
    for want, label, lines in cases:
        p = write(lines)
        paths.append(p)
        got = substantive(p)
        if got != want:
            failures.append(f"{label}: wanted {'queue' if want else 'skip'}, got {'queue' if got else 'skip'}")

    # A missing or unreadable transcript must fail OPEN: losing real signal is
    # worse than one wasted drain run.
    missing = Path(tempfile.gettempdir()) / "compound-nonexistent-transcript.jsonl"
    if not substantive(missing):
        failures.append("missing transcript should fail open (queue it)")

    garbage = write([])
    garbage.write_text("this is not jsonl at all\n{oops\n")
    if not substantive(garbage):
        failures.append("unparseable transcript should fail open (queue it)")
    paths.append(garbage)

    for p in paths:
        p.unlink(missing_ok=True)

    corpus_ok, corpus_n, corpus_errs = real_corpus()
    failures.extend(corpus_errs)

    total = len(cases) + 2 + corpus_n
    if failures:
        print(f"FAIL {len(failures)}/{total}")
        for f in failures:
            print("  -", f)
        return 1
    print(f"PASS {total}/{total} fixtures "
          f"({len(cases) + 2} synthetic, {corpus_n} real parked sessions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
