#!/usr/bin/env python3
"""Fixtures for digest_transcript.py. Run: python3 tests/test_digest_transcript.py

Covers the two defects the 2026-09-12 rewrite fixed -- an empty digest for
Codex transcripts, and head-biased truncation that dropped the end of any
session over budget -- plus the dedupe rules that made the budget affordable.
"""
import json
import os
import sys
import tempfile

# digest_transcript lives in the scripts dir, a sibling of tests/ in the repo.
# COMPOUND_SKILL_DIR overrides that when the scripts are installed elsewhere.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.environ.get("COMPOUND_SKILL_DIR") or os.path.join(_REPO_ROOT, "scripts")
sys.path.insert(0, SKILL_DIR)
import digest_transcript as dt

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


def write(records):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


def cc_user(text):
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


def cc_asst(text, tools=None):
    content = [{"type": "text", "text": text}]
    for t in tools or []:
        content.append({"type": "tool_use", "name": t})
    return {"type": "assistant", "message": {"content": content}}


def cc_err(text):
    return {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "is_error": True,
                 "content": [{"type": "text", "text": text}]}
            ]
        },
    }


def digest(records):
    src = write(records)
    out = src + ".md"
    rc = dt.main(src, out)
    body = open(out).read()
    os.unlink(src)
    os.unlink(out)
    return rc, body


print("digest_transcript fixtures")

# --- schema coverage -------------------------------------------------------
rc, body = digest([cc_user("fix the drain"), cc_asst("on it", ["Bash"])])
check("claude-code: user text kept", "fix the drain" in body)
check("claude-code: assistant text kept", "on it" in body)
check("claude-code: tool name kept", "[tools: Bash]" in body)

codex = [
    {"type": "session_meta", "payload": {"instructions": "codex run"}},
    {"type": "response_item", "payload": {"type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "port it to codex"}]}},
    {"type": "response_item", "payload": {"type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "ported"}]}},
    {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
    {"type": "response_item", "payload": {"type": "reasoning", "content": "hidden"}},
]
rc, body = digest(codex)
check("codex: user text kept", "port it to codex" in body)
check("codex: assistant text kept", "ported" in body)
check("codex: tool name kept", "[tools: shell]" in body)
check("codex: reasoning dropped", "hidden" not in body)

rc, body = digest([
    {"type": "response_item", "payload": {"type": "message", "role": "developer",
        "content": [{"type": "input_text", "text": "SYSTEMPROMPTFURNITURE"}]}},
    cc_user("real ask"),
])
check("codex: developer-role furniture dropped", "SYSTEMPROMPTFURNITURE" not in body)

# --- noise + payload rules -------------------------------------------------
rc, body = digest([cc_user("<system-reminder>noise</system-reminder>"), cc_user("real")])
check("hook noise dropped", "noise" not in body and "real" in body)

rc, body = digest([
    cc_user("q"),
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": False,
         "content": [{"type": "text", "text": "BIGFILEPAYLOAD"}]}]}},
    cc_err("boom: exit 1"),
])
check("successful tool payload dropped", "BIGFILEPAYLOAD" not in body)
check("tool error kept", "boom: exit 1" in body)

# --- dedupe ----------------------------------------------------------------
pr = {"type": "pr-link", "prRepository": "example-repo", "prNumber": 1, "prUrl": "u"}
rc, body = digest([pr] * 50 + [cc_user("q")])
check("repeated pr-link deduped to one", body.count("[pr-link]") == 1,
      f"count={body.count('[pr-link]')}")

tool_only = {"type": "assistant",
             "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}
rc, body = digest([cc_user("q")] + [tool_only] * 5)
check("consecutive identical tool lines collapsed",
      "[tools: Bash] x5" in body, body[-80:])

# --- budget fitting --------------------------------------------------------
small = [cc_user("a"), cc_asst("b")]
rc, body = digest(small)
check("under budget: nothing elided", "[DIGEST:" not in body)

# Over budget, with a distinctive marker at the very end of the session.
big = []
for i in range(4000):
    big.append(cc_asst("narration " + "z" * 300))
    if i % 40 == 0:
        big.append(cc_user(f"USERTURN-{i}"))
big.append(cc_user("FINAL USER TURN"))
big.append(cc_err("FINAL ERROR"))
rc, body = digest(big)
check("over budget: elision header present", "[DIGEST:" in body)
check("over budget: every user turn kept", body.count("USERTURN-") == 100,
      f"count={body.count('USERTURN-')}")
check("over budget: LAST user turn kept", "FINAL USER TURN" in body)
check("over budget: LAST error kept", "FINAL ERROR" in body)
check("over budget: earliest turn also kept (coverage spans session)",
      "USERTURN-0" in body)
check("over budget: respects the cap", len(body) <= dt.TOTAL_CAP + 4096,
      f"len={len(body)}")

# Pathological: user turns alone overflow. The ending must still survive.
huge = [cc_user("u" * 5000) for _ in range(200)]
huge.append(cc_user("TAIL SENTINEL"))
rc, body = digest(huge)
check("user-only overflow: tail sentinel survives", "TAIL SENTINEL" in body)
check("user-only overflow: respects the cap", len(body) <= dt.TOTAL_CAP + 4096,
      f"len={len(body)}")

# --- usability signal ------------------------------------------------------
rc, body = digest([{"type": "response_item", "payload": {"type": "reasoning"}}])
check("empty digest signals park (exit 2)", rc == 2, f"rc={rc}")
rc, body = digest([cc_user("x" * 5000)])
check("usable digest signals ok (exit 0)", rc == 0, f"rc={rc}")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)

# --- input == output must be refused, never truncated -------------------------
# The drain's retry path handed this script the same path twice: a marker already
# digested has transcript_path pointing AT the digest. Opening the output
# truncated it to zero before a line was read, destroying the session's only
# reduced copy. Measured 2026-09-13: 108689 bytes -> 0. Three independent audit
# agents found it from different angles, which is what a silent data-loss bug
# looks like from the outside.
import os as _os
import subprocess as _sp
import tempfile as _tf

_d = _tf.mkdtemp(prefix="digest-idem-")
_src = _os.path.join(_d, "t.jsonl")
with open(_src, "w") as _fh:
    for _i in range(400):
        _fh.write(json.dumps({"type": "user",
                              "message": {"role": "user", "content": f"turn {_i} with content"}}) + "\n")
        _fh.write(json.dumps({"type": "assistant",
                              "message": {"role": "assistant", "content": [
                                  {"type": "tool_use", "name": "Bash", "input": {}},
                                  {"type": "text", "text": "y" * 200}]}}) + "\n")
_out = _os.path.join(_d, "digest.md")
_script = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "digest_transcript.py")
if not _os.path.exists(_script):
    _script = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                            "scripts", "digest_transcript.py")
_sp.run([sys.executable, _script, _src, _out], capture_output=True)
_before = _os.path.getsize(_out)
assert _before > 1000, f"setup failed: first digest was {_before} bytes"
_r = _sp.run([sys.executable, _script, _out, _out], capture_output=True, text=True)
_after = _os.path.getsize(_out)
assert _after == _before, (
    f"re-digesting in place destroyed the digest: {_before} -> {_after} bytes")
assert _r.returncode != 0, "input == output must be refused with a non-zero exit"
assert "same file" in (_r.stderr or ""), f"expected a clear refusal, got: {_r.stderr!r}"
import shutil as _sh
_sh.rmtree(_d, ignore_errors=True)

print("all fixtures pass")
