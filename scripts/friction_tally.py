#!/usr/bin/env python3
"""Count friction in a session transcript: transcript path -> JSON tally.

Friction is what a person pays for twice: a command that keeps failing, a
correction they had to repeat, a known trap (alembic heads, a rebase conflict, a
red CI check after a push, the linter's reviewed-date rule). Detection is
deterministic. Nothing here calls a model, and the named traps and correction
phrases are DATA in friction_patterns.json, so adding one needs no code.

Both transcript schemas are read, as digest_transcript.py reads them: Claude Code
({"type": "user"|"assistant"}) and Codex rollouts ({"type": "response_item"}).

    friction_tally.py TRANSCRIPT [--session ID]     print the tally as JSON

Library:
    tally(path, session_id=None, patterns=None) -> dict          pure, whole file
    scan_incremental(path, cursor, ...) -> (dict, cursor)        only the new lines

The incremental form keeps a cursor (byte offset, line number and the scanner's
own counters), so a loop that spans several turns is one event with the right
count and a Stop hook never rereads a 100 MB transcript.

An event is {type, signature, count, evidence: {path, line}}, where type is one
of failed_command, retry_loop, user_correction, named_pattern. A command that
failed once or twice is a failed_command. Run three or more times with at least
two failures it is a retry_loop (retries = count - 1), and replaces the
failed_command for that signature, so a loop is one event. A command whose
output matched a named pattern is reported as that pattern, not as a failure.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

PATTERNS_PATH = Path(__file__).resolve().parent / "friction_patterns.json"
RETRY_MIN_RUNS = 3
RETRY_MIN_FAILS = 2
SHELL_TOOLS = {"bash", "shell", "exec_command", "local_shell", "shell_command", "container.exec"}
NOISE = ("<system-reminder>", "<command-name>", "Caveat: The messages below", "<task-notification>",
         "<local-command-stdout>")
SIG_CAP = 160

_cache: dict = {}


def load_patterns(path: Path | None = None) -> dict:
    p = Path(path) if path else PATTERNS_PATH
    if p not in _cache:
        try:
            raw = json.loads(p.read_text())
        except (OSError, ValueError):
            raw = {}
        _cache[p] = {"corrections": raw.get("corrections") or [], "named": raw.get("named") or []}
    return _cache[p]


# -- normalizing a command into a signature -------------------------------------
_CD = re.compile(r"^\s*cd\s+\S+\s*(?:&&|;)\s*")
_ENV = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
_QUOTED = re.compile(r'"[^"]*"|\'[^\']*\'')
_FILE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_HEX = re.compile(r"[0-9a-f]{7,40}")


def normalize(cmd: str) -> str:
    """Strip what varies between runs of the same intent: cwd, env values, paths, literals."""
    c = " ".join(str(cmd).split())
    while _CD.match(c):
        c = _CD.sub("", c, count=1)
    c = _ENV.sub("", c)
    c = _QUOTED.sub("<str>", c)
    out = []
    for i, tok in enumerate(c.split(" ")):
        if i == 0 and "/" in tok:
            tok = tok.rsplit("/", 1)[-1]
        if tok.startswith("-"):
            tok = tok.split("=", 1)[0] + ("=<v>" if "=" in tok else "")
        elif re.match(r"^[a-z][a-z0-9+.-]*://", tok):
            tok = "<url>"
        elif "/" in tok or (_FILE.search(tok) and re.search(r"[A-Za-z_]", tok.split(".")[0] or "")):
            tok = "<path>"
        elif tok.isdigit():
            tok = "N"
        elif _HEX.fullmatch(tok):
            tok = "<sha>"
        out.append(tok)
    return " ".join(out)[:SIG_CAP]


# -- reading either schema ----------------------------------------------------------
def _flat(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _codex_command(payload: dict) -> str | None:
    cmd = None
    args = payload.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = None
    if isinstance(args, dict):
        cmd = args.get("command") if args.get("command") is not None else args.get("cmd")
    if cmd is None and isinstance(payload.get("action"), dict):
        cmd = payload["action"].get("command")
    if isinstance(cmd, list):
        if len(cmd) >= 3 and cmd[1] in ("-lc", "-c"):
            return str(cmd[2])
        return " ".join(str(x) for x in cmd)
    return cmd if isinstance(cmd, str) else None


def events_in(rec: dict):
    """Yield normalized records: ('session', id) ('call', id, cmd) ('result', id, text, failed) ('user', text)."""
    rtype = rec.get("type")
    if rtype == "assistant":
        for b in (rec.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and str(b.get("name", "")).lower() in SHELL_TOOLS:
                cmd = (b.get("input") or {}).get("command")
                if isinstance(cmd, str):
                    yield ("call", "claude", b.get("id"), cmd)
    elif rtype == "user":
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str):
            yield ("user", "claude", content)
            return
        for b in content or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                text = _flat(b.get("content")) if not isinstance(b.get("content"), str) else b["content"]
                m = re.match(r"\s*Exit code (\d+)", text)
                failed = bool(b.get("is_error")) or bool(m and m.group(1) != "0")
                yield ("result", "claude", b.get("tool_use_id"), text, failed)
            elif b.get("type") == "text":
                yield ("user", "claude", b.get("text", ""))
    elif rtype == "session_meta":
        sid = (rec.get("payload") or {}).get("id")
        if sid:
            yield ("session", "codex", str(sid))
    elif rtype == "response_item":
        p = rec.get("payload") or {}
        pt = p.get("type")
        if pt in ("function_call", "custom_tool_call", "local_shell_call"):
            cmd = _codex_command(p)
            if cmd is not None:
                yield ("call", "codex", p.get("call_id") or p.get("id"), cmd)
        elif pt in ("function_call_output", "custom_tool_call_output"):
            out = p.get("output")
            body, code = out if isinstance(out, str) else "", None
            if isinstance(out, str):
                try:
                    out = json.loads(out)
                except ValueError:
                    out = None
            if isinstance(out, dict):
                body = str(out.get("output") or out.get("content") or "")
                code = out.get("exit_code")
                if code is None:
                    code = (out.get("metadata") or {}).get("exit_code")
            failed = code not in (0, None) or bool(isinstance(out, dict) and out.get("is_error"))
            yield ("result", "codex", p.get("call_id"), body, failed)
        elif pt == "message" and p.get("role") == "user":
            text = "\n".join(b.get("text", "") for b in p.get("content") or [] if isinstance(b, dict))
            yield ("user", "codex", text)


# -- the scanner ---------------------------------------------------------------------------
def fresh_state() -> dict:
    return {"calls": {}, "after": {}, "sigs": {}, "named": {}, "corr": {}, "commands": 0, "failed": 0,
            "schema": None, "session": None}


class Scanner:
    def __init__(self, patterns: dict | None = None, state: dict | None = None):
        self.patterns = patterns if patterns is not None else load_patterns()
        self.s = state if state is not None else fresh_state()
        self._rx: dict = {}

    def _re(self, pat: str, flags=0):
        k = (pat, flags)
        if k not in self._rx:
            self._rx[k] = re.compile(pat, flags)
        return self._rx[k]

    def feed(self, line_no: int, rec: dict) -> None:
        s = self.s
        for ev in events_in(rec):
            kind = ev[0]
            if kind != "session" and s["schema"] is None:
                s["schema"] = ev[1]
            if kind == "session":
                s["schema"], s["session"] = "codex", ev[2]
            elif kind == "call":
                _, _, cid, cmd = ev
                if cid is not None:
                    s["calls"][str(cid)] = cmd
                    if len(s["calls"]) > 500:
                        s["calls"].pop(next(iter(s["calls"])))
                for n in self.patterns["named"]:
                    if n.get("after_command") and self._re(n["after_command"]).search(cmd):
                        s["after"][n["id"]] = True
            elif kind == "result":
                self._result(line_no, ev[2], ev[3], ev[4])
            elif kind == "user":
                self._user(line_no, ev[2])

    def _result(self, line_no, cid, text, failed) -> None:
        s = self.s
        cmd = s["calls"].pop(str(cid), None)
        if cmd is None:
            return
        s["commands"] += 1
        matched = False
        for n in self.patterns["named"]:
            if n.get("only_failed") and not failed:
                continue
            if n.get("when_command") and not self._re(n["when_command"]).search(cmd):
                continue
            if n.get("after_command") and not s["after"].get(n["id"]):
                continue
            for m in self._re(n["regex"]).finditer(text):
                sig = n.get("signature", n["id"]).format(**m.groupdict()) if m.groupdict() else n.get("signature", n["id"])
                d = s["named"].setdefault(sig, {"count": 0, "line": line_no, "pattern": n["id"]})
                d["count"] += 1
                matched = True
        sig = normalize(cmd)
        d = s["sigs"].setdefault(sig, {"runs": 0, "fails": 0, "line": None})
        d["runs"] += 1
        if failed and not matched:
            s["failed"] += 1
            d["fails"] += 1
            if d["line"] is None:
                d["line"] = line_no

    def _user(self, line_no, text) -> None:
        if not text or any(m in text for m in NOISE):
            return
        low = text.lower()
        for c in self.patterns["corrections"]:
            if self._re(c["regex"]).search(low):
                d = self.s["corr"].setdefault(c["id"], {"count": 0, "line": line_no})
                d["count"] += 1

    def result(self, path, session_id: str | None) -> dict:
        s = self.s
        events = []
        for sig, d in s["sigs"].items():
            if d["fails"] >= RETRY_MIN_FAILS and d["runs"] >= RETRY_MIN_RUNS:
                events.append({"type": "retry_loop", "signature": sig, "count": d["runs"], "retries": d["runs"] - 1,
                               "evidence": {"path": str(path), "line": d["line"]}})
            elif d["fails"]:
                events.append({"type": "failed_command", "signature": sig, "count": d["fails"],
                               "evidence": {"path": str(path), "line": d["line"]}})
        for sig, d in s["named"].items():
            events.append({"type": "named_pattern", "signature": sig, "count": d["count"], "pattern": d["pattern"],
                           "evidence": {"path": str(path), "line": d["line"]}})
        for cid, d in s["corr"].items():
            events.append({"type": "user_correction", "signature": f"correction:{cid}", "count": d["count"],
                           "evidence": {"path": str(path), "line": d["line"]}})
        events.sort(key=lambda e: (e["evidence"]["line"] or 0, e["signature"]))
        return {"session_id": session_id or s["session"] or Path(str(path)).stem, "transcript_path": str(path),
                "schema": s["schema"] or "unknown", "commands": s["commands"], "failed": s["failed"], "events": events}


def scan_incremental(path, cursor: dict | None, patterns: dict | None = None,
                     session_id: str | None = None) -> tuple[dict, dict]:
    """Scan only the lines added since `cursor`; return (cumulative tally, new cursor)."""
    cur = cursor or {"offset": 0, "line": 0, "state": None}
    try:
        size = os.path.getsize(path)
    except OSError:
        return Scanner(patterns).result(path, session_id), cur
    if size < cur["offset"]:
        cur = {"offset": 0, "line": 0, "state": None}
    sc = Scanner(patterns, cur["state"])
    offset, line_no = cur["offset"], cur["line"]
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break
                offset += len(raw)
                line_no += 1
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    sc.feed(line_no, rec)
    except OSError:
        pass
    return sc.result(path, session_id), {"offset": offset, "line": line_no, "state": sc.s}


def tally(path, session_id: str | None = None, patterns: dict | None = None) -> dict:
    return scan_incremental(path, None, patterns, session_id)[0]


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        sys.stderr.write(__doc__.split("\n\n")[1] + "\n")
        return 2
    sid = argv[argv.index("--session") + 1] if "--session" in argv else None
    print(json.dumps(tally(argv[0], sid), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
