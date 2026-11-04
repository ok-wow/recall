#!/usr/bin/env python3
"""friction_tally: transcript path -> JSON tally, in both host schemas.

Fixtures in tests/fixtures/friction are hand-written from the two schemas that
digest_transcript.py documents. Expected line numbers were counted by hand from
the fixture files, not computed by the code under test.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import FIX, check, clean, finish, tmp  # noqa: E402

import friction_tally as ft  # noqa: E402


def ev(t, **kw):
    return {(e["type"], e["signature"]): e for e in t["events"]}


print("friction_tally")

# -- failed commands, retry loops, normalization -------------------------------
t = ft.tally(FIX / "claude_loop.jsonl")
e = ev(t)
loop = [x for x in t["events"] if x["type"] == "retry_loop"]
check("claude: a 4x failing command is ONE retry_loop event with count 4",
      len(loop) == 1 and loop[0]["count"] == 4, str(t["events"]))
check("claude: runs of 'pytest tests/test_a.py' and 'cd /repo && pytest tests/test_b.py' share a signature",
      loop and loop[0]["signature"] == "pytest <path> -x", str(loop))
check("claude: the loop is not also reported as separate failed_command events",
      not [x for x in t["events"] if x["type"] == "failed_command"], str(t["events"]))
check("claude: evidence points at the first failing result, by line number",
      loop and loop[0]["evidence"] == {"path": str(FIX / "claude_loop.jsonl"), "line": 3}, str(loop))
check("claude: cost proxy is retries (count - 1)", loop and loop[0]["retries"] == 3, str(loop))
check("claude: a command that keeps SUCCEEDING (git status x4) is not friction",
      not [x for x in t["events"] if "git" in x["signature"]], str(t["events"]))
check("claude: session id comes from the file name", t["session_id"] == "claude_loop", t["session_id"])

t = ft.tally(FIX / "claude_two_fails.jsonl")
check("claude: two failures are a failed_command with count 2, not yet a retry loop",
      [(x["type"], x["count"]) for x in t["events"]] == [("failed_command", 2)], str(t["events"]))

# -- named friction patterns (data table) -------------------------------------------
t = ft.tally(FIX / "claude_patterns.jsonl")
named = {x["signature"]: x for x in t["events"] if x["type"] == "named_pattern"}
check("pattern: alembic multiple heads", "alembic-multiple-heads" in named, str(sorted(named)))
check("pattern: git rebase conflict", "git-conflict" in named, str(sorted(named)))
check("pattern: push followed by a failing CI check names the check",
      "ci-check-failed:ci-python" in named and "ci-check-failed:lint" not in named, str(sorted(named)))
check("pattern: linter agents-reviewed date failure", "linter-agents-reviewed-date" in named, str(sorted(named)))
check("pattern: evidence lines are the result lines (3, 5, 9, 11)",
      [named[s]["evidence"]["line"] for s in
       ("alembic-multiple-heads", "git-conflict", "ci-check-failed:ci-python", "linter-agents-reviewed-date")]
      == [3, 5, 9, 11], str({s: n["evidence"] for s, n in named.items()}))
check("pattern: a command that matched a named pattern is not double-reported as failed_command",
      not [x for x in t["events"] if x["type"] == "failed_command"], str(t["events"]))

t = ft.tally(FIX / "claude_quiet.jsonl")
check("quiet: successful alembic, grep for CONFLICT, passing checks, assistant 'again' -> zero events",
      t["events"] == [], str(t["events"]))
t = ft.tally(FIX / "claude_ci_nopush.jsonl")
check("pattern: a failing check with NO earlier push is not a post-push CI failure",
      not [x for x in t["events"] if x["signature"].startswith("ci-check-failed")], str(t["events"]))

# -- user corrections -------------------------------------------------------------------
t = ft.tally(FIX / "claude_corrections.jsonl")
corr = {x["signature"]: x for x in t["events"] if x["type"] == "user_correction"}
check("corrections: the five phrases are each detected, from user text only",
      sorted(corr) == ["correction:again", "correction:already-told-you", "correction:as-i-said",
                       "correction:we-agreed", "correction:you-keep-doing"], str(sorted(corr)))
check("corrections: a repeated phrase is one event with a count",
      corr["correction:you-keep-doing"]["count"] == 2 and corr["correction:again"]["count"] == 1, str(corr))
check("corrections: evidence is the first line it appeared on",
      corr["correction:you-keep-doing"]["evidence"]["line"] == 5, str(corr["correction:you-keep-doing"]))

# -- Codex rollout schema ----------------------------------------------------------------
t = ft.tally(FIX / "codex_mixed.jsonl")
loop = [x for x in t["events"] if x["type"] == "retry_loop"]
check("codex: 3 failing runs (exit_code 1) of one command are one retry_loop, count 3",
      len(loop) == 1 and loop[0]["count"] == 3 and loop[0]["signature"] == "pytest <path> -x", str(t["events"]))
check("codex: alembic multiple heads detected from exit-255 output",
      any(x["signature"] == "alembic-multiple-heads" and x["evidence"]["line"] == 10 for x in t["events"]),
      str(t["events"]))
check("codex: user correction detected, assistant 'trying again' ignored",
      [x["signature"] for x in t["events"] if x["type"] == "user_correction"] == ["correction:as-i-said"],
      str(t["events"]))
check("codex: session id comes from session_meta", t["session_id"] == "cdx-session-1", t["session_id"])
check("codex: schema is reported", t["schema"] == "codex", t["schema"])

# -- purity and fail-open ---------------------------------------------------------------------
check("pure: same input, identical output", ft.tally(FIX / "claude_loop.jsonl") == ft.tally(FIX / "claude_loop.jsonl"))
d = tmp()
bad = d / "bad.jsonl"
bad.write_text('not json\n{"type":"user","message":{"content":"you keep doing it"}}\n\x00\x01\n')
check("fail open: garbage lines are skipped, the good one still counts",
      [x["signature"] for x in ft.tally(bad)["events"]] == ["correction:you-keep-doing"])
check("fail open: a missing file is an empty tally, not an exception", ft.tally(d / "nope.jsonl")["events"] == [])

# -- patterns are DATA ----------------------------------------------------------------------------
custom = {"corrections": [], "named": [{"id": "foobar-broke", "source": "tool_output",
                                         "regex": "FOOBAR exploded", "only_failed": False}]}
cf = d / "c.jsonl"
cf.write_text(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "x", "name": "Bash", "input": {"command": "foobar"}}]}}) + "\n" +
    json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "x", "content": "FOOBAR exploded"}]}}) + "\n")
check("data: a pattern added to the table (no code change) is detected",
      [x["signature"] for x in ft.tally(cf, patterns=custom)["events"]] == ["foobar-broke"])
check("data: the shipped table is JSON under scripts/", (Path(ft.__file__).parent / "friction_patterns.json").is_file())

# -- incremental cursor --------------------------------------------------------------------------------
lines = (FIX / "claude_loop.jsonl").read_text().splitlines(keepends=True)
part = d / "turns.jsonl"
part.write_text("".join(lines[:5]))                      # 2 failing runs so far
r1, cur = ft.scan_incremental(part, None)
check("cursor: first turn sees 2 failures", [(x["type"], x["count"]) for x in r1["events"]] == [("failed_command", 2)], str(r1["events"]))
r1b, cur_b = ft.scan_incremental(part, cur)
check("cursor: re-scanning with nothing new changes nothing (no double count)",
      r1b["events"] == r1["events"] and cur_b["offset"] == cur["offset"], str(r1b["events"]))
with part.open("a") as fh:
    fh.write("".join(lines[5:9]))                        # runs 3 and 4
r2, cur2 = ft.scan_incremental(part, cur_b)
check("cursor: second turn carries the loop across turns, count 4 (not 2+4)",
      [(x["type"], x["count"]) for x in r2["events"]] == [("retry_loop", 4)], str(r2["events"]))
with part.open("a") as fh:
    fh.write(lines[9][: len(lines[9]) // 2])             # a half-flushed line
r3, cur3 = ft.scan_incremental(part, cur2)
check("cursor: a half-written trailing line is not consumed", cur3["offset"] == cur2["offset"], str(cur3["offset"]))
full = d / "full.jsonl"
shutil.copy(FIX / "claude_loop.jsonl", full)
check("cursor: incremental over the whole file equals the pure tally",
      ft.scan_incremental(full, None)[0]["events"] == ft.tally(full)["events"])

clean(d)
sys.exit(finish("friction_tally checks"))
