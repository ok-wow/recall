#!/usr/bin/env python3
"""install.sh must wire both hosts, and uninstall must put each back.

Claude Code and Codex take the same hook structure --
  {"hooks": {"SessionEnd": [{"hooks": [{"type","command","timeout"}]}]}}
-- in different files (settings.json vs hooks.json). That similarity is why
one installer serves both, and it is also why a host bug is easy to miss: the
Claude path passing says nothing about the Codex path.

Runs the REAL installer against a throwaway HOME with launchctl shimmed, so
nothing is registered on the machine running the tests.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EVENTS = {"SessionEnd", "SessionStart", "UserPromptSubmit", "PostToolUse"}
# PreCompact exists in Claude Code and not in Codex, whose event list is
# PreToolUse/SessionStart/UserPromptSubmit/Stop/SessionEnd/PostToolUse. Wiring
# an event a host never fires would look installed and do nothing.
HOST_ONLY = {"claude": {"PreCompact"}, "codex": set()}

fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        fails.append(f"{label}{(' — ' + detail) if detail else ''}")


def run(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{home / 'bin'}:{os.environ['PATH']}",
        "RECALL_HOME": str(home / "state"),
    }
    return subprocess.run(["sh", "-c", f"yes y | {REPO}/install.sh " + " ".join(args)],
                          capture_output=True, text=True, env=env, cwd=REPO)


def hosts_case(host: str, config_rel: str, seed: str) -> None:
    home = Path(tempfile.mkdtemp())
    try:
        cfg = home / config_rel
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(seed)
        (home / "bin").mkdir()
        shim = home / "bin" / "launchctl"       # never touch the real launchd
        shim.write_text("#!/bin/sh\nexit 0\n")
        shim.chmod(0o755)

        p = run(home, "--host", host)
        check(f"[{host}] install exits 0", p.returncode == 0, p.stderr[-200:])
        check(f"[{host}] plan names the host", f"host: {host}" in p.stdout, p.stdout[:120])

        d = json.loads(cfg.read_text())
        got = set(d.get("hooks", {}))
        want = EVENTS | HOST_ONLY[host]
        check(f"[{host}] events registered in {config_rel}", got == want, str(sorted(got)))
        # The guard that matters: Codex must NOT be wired for an event it has no
        # concept of, and Claude MUST be wired for the one only it can fire.
        for other, only in HOST_ONLY.items():
            if other != host:
                for ev in only:
                    check(f"[{host}] does not register {ev}", ev not in got)
        cmds = [h["command"] for v in d.get("hooks", {}).values()
                for e in v for h in e.get("hooks", [])]
        check(f"[{host}] commands point into the repo", all(str(REPO) in c for c in cmds))

        link = home / config_rel.split("/")[0] / "skills" / "compound"
        check(f"[{host}] skill linked", link.is_symlink())
        check(f"[{host}] skill readable through the link",
              (link / "SKILL.md").is_file())

        p2 = run(home, "--host", host, "--uninstall")
        check(f"[{host}] uninstall exits 0", p2.returncode == 0, p2.stderr[-200:])
        d2 = json.loads(cfg.read_text())
        check(f"[{host}] hooks removed", not d2.get("hooks"), str(d2.get("hooks")))
        check(f"[{host}] skill unlinked", not link.exists())
        # State is the user's corpus; an uninstall that deletes it is not an uninstall.
        check(f"[{host}] catalogs survive uninstall",
              (home / "state" / "catalogs" / "FAILURE_MODES.yaml").is_file())
    finally:
        shutil.rmtree(home, ignore_errors=True)


hosts_case("claude", ".claude/settings.json", "{}")
hosts_case("codex", ".codex/hooks.json", '{"hooks":{}}')

# An unknown host must be refused, not silently treated as Claude.
_h = Path(tempfile.mkdtemp())
try:
    (_h / "bin").mkdir()
    bad = run(_h, "--host", "emacs")
    check("unknown host is refused", bad.returncode != 0, f"rc={bad.returncode}")
    check("refusal names the valid hosts", "claude or codex" in (bad.stderr + bad.stdout))
finally:
    shutil.rmtree(_h, ignore_errors=True)

# The fresh-clone CI job keeps its own copy of the claude event list, because it
# cannot import this file without running it. A copy drifts: main sat red for a
# week after the witness hook landed here and not there. So the suite reads the
# workflow and fails the moment the two disagree, which is when a person can
# still see why.
_wf = (REPO / ".github" / "workflows" / "test.yml").read_text()
_m = re.search(r"want = \{([^}]*)\}", _wf)
_ci = set(re.findall(r'"([A-Za-z]+)"', _m.group(1))) if _m else set()
check(
    "fresh-clone CI job expects the same claude events as this suite",
    _ci == EVENTS | HOST_ONLY["claude"],
    f"workflow has {sorted(_ci)}",
)

TOTAL = 11 * 2 + 2 + 1
if fails:
    print(f"FAIL {len(fails)} check(s):")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASS {TOTAL}/{TOTAL} host-install checks (claude + codex, install and uninstall)")
