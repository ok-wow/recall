"""Shared plumbing for the capture-visibility suites. Not a test (run.py globs test_*.py)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
HOOKS = REPO / "hooks"
FIX = REPO / "tests" / "fixtures" / "friction"
sys.path.insert(0, str(SCRIPTS))

FAILS: list[str] = []
TOTAL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    if not ok:
        FAILS.append(name)
        print(f"  [FAIL] {name}  {detail}"[:600])
    else:
        print(f"  [ok ] {name}")


def finish(label: str) -> int:
    if FAILS:
        print(f"FAIL {len(FAILS)}/{TOTAL} {label}")
        return 1
    print(f"PASS {TOTAL}/{TOTAL} {label}")
    return 0


def tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="recalltest-"))


def clean(*paths: Path) -> None:
    for p in paths:
        shutil.rmtree(p, ignore_errors=True)


def env_for(home: Path, **extra: str) -> dict:
    """An environment that cannot reach the real home, tokens or sync settings."""
    e = {k: v for k, v in os.environ.items()
         if not k.startswith(("RECALL_", "GH_", "GITHUB_", "CLAUDE_PLUGIN"))}
    e.update(HOME=str(home), RECALL_HOME=str(home / "state"), RECALL_HOST_DIR=str(home / "host"),
             RECALL_HOST="testhost", GIT_TERMINAL_PROMPT="0")
    e.update(extra)
    return e


def run(cmd: list, env: dict, stdin: str = "", cwd: Path | None = None, timeout: int = 60):
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=env,
                          cwd=cwd, timeout=timeout)


def py(script: str, *args: str, env: dict, stdin: str = ""):
    return run([sys.executable, str(SCRIPTS / script), *args], env, stdin)


def hook_py(name: str, payload: dict, env: dict):
    return run([sys.executable, str(HOOKS / name)], env, json.dumps(payload))


def hook_sh(name: str, payload: dict, env: dict):
    return run(["bash", str(HOOKS / name)], env, json.dumps(payload))


def jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    return p.stdout.strip()


def bare_remote(root: Path) -> Path:
    """A bare repo whose `main` already holds one commit, standing in for recall-personal."""
    bare, seed = root / "remote.git", root / "seed"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "README").write_text("personal\n")
    for a in (["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"],
              ["remote", "add", "origin", str(bare)], ["push", "-q", "origin", "main"]):
        subprocess.run(["git", "-C", str(seed), *a], check=True, capture_output=True)
    return bare


def cc_session(path: Path, n_tools: int = 4, extra: list | None = None) -> Path:
    """A substantive Claude Code transcript: enough turns and tool calls to be queued."""
    recs = []
    for i in range(n_tools):
        recs.append({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": f"step {i}"}]}})
        recs.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": f"doing step {i}"},
            {"type": "tool_use", "id": f"s{i}", "name": "Bash", "input": {"command": "ls"}}]}})
        recs.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"s{i}", "content": "ok"}]}})
    recs += extra or []
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return path
