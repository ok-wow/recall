#!/usr/bin/env python3
"""The plugin's hooks run from a cached copy, and never write inside it.

Claude Code copies a plugin to plugins/cache/<marketplace>/<plugin>/<version>/
and swaps that folder on every update, so anything written there is lost at the
next commit. This copies the repo to such a path with every executable bit
removed, runs each hook exactly as hooks/hooks.json declares it, and then
requires the copy to be unchanged: every write has to land under RECALL_HOME.

It also pins both guards against having install.sh AND the plugin at once,
because hooks registered by both fire twice: install.sh refuses, and the
plugin's SessionStart says so in one line.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS = json.loads((REPO / "hooks" / "hooks.json").read_text())["hooks"]
PLACEHOLDER = "${CLAUDE_PLUGIN_ROOT}"
TOKEN = "allow-same-origin-sandbox-flag"      # long enough to fire on its own

fails: list[str] = []
ran = [0]


def check(label: str, ok: bool, detail: str = "") -> None:
    ran[0] += 1
    if not ok:
        fails.append(f"{label}{(' — ' + str(detail)[-300:]) if detail else ''}")


def clean_env(home: Path) -> dict:
    # A developer's own RECALL_HOME must never receive fixture writes, and a
    # bytecode setting in the shell would hide the writes this suite looks for.
    drop = ("RECALL_", "OKWOW_", "CLAUDE_", "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")
    env = {k: v for k, v in os.environ.items() if not k.startswith(drop)}
    env["HOME"] = str(home)
    return env


def make_copy(base: Path) -> Path:
    root = base / "plugins" / "cache" / "ok-wow" / "recall" / "0123456789ab"
    shutil.copytree(REPO, root, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            p.chmod(0o644)            # prove no hook needs an executable bit
    return root


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.lstat().st_size, p.lstat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}


def run_hook(event: str, root: Path, env: dict, payload: dict | None = None,
             substitute: bool = True) -> subprocess.CompletedProcess:
    """Run the hook the way a host does: the command string through a shell.

    substitute=True is Claude Code, which writes the path into the string.
    substitute=False is a host that only exports the variable (Codex does).
    """
    cmd = HOOKS[event][0]["hooks"][0]["command"]
    if substitute:
        cmd = cmd.replace(PLACEHOLDER, str(root))
    return subprocess.run(["/bin/sh", "-c", cmd], input=json.dumps(payload or {}),
                          capture_output=True, text=True, timeout=60,
                          env={**env, "CLAUDE_PLUGIN_ROOT": str(root)})


def transcript(path: Path, extra: str = "") -> Path:
    rows = []
    for i in range(3):
        rows.append({"type": "user", "message": {"role": "user", "content": f"step {i} {extra}"}})
        rows.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "true"}}]}})
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def wait_for(*paths: Path, seconds: float = 10.0) -> None:
    end = time.time() + seconds
    while time.time() < end and not any(p.exists() for p in paths):
        time.sleep(0.1)


def install_settings_hooks(home: Path, clone: str) -> None:
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "timeout": 10,
                                     "command": f"{clone}/hooks/recall-sessionstart.sh"}]}]}}))


# ------------------------------------------------ hooks from the cached copy --
base = Path(tempfile.mkdtemp())
try:
    root = make_copy(base)
    before = snapshot(root)
    home = base / "home"
    home.mkdir()
    env = clean_env(home)
    state = home / ".recall"

    p = run_hook("SessionStart", root, env)
    check("SessionStart runs from the copy", p.returncode == 0, p.stderr)
    for c in ("FAILURE_MODES", "PROCESS_FAILURES", "DECISIONS"):
        check(f"first SessionStart seeds {c}.yaml under ~/.recall",
              (state / "catalogs" / f"{c}.yaml").is_file())
    for d in ("pending", "processed", "quarantine", "digests", "probe-state"):
        check(f"first SessionStart creates ~/.recall/{d}", (state / d).is_dir())
    check("no double-registration line without install.sh hooks", "runs twice" not in p.stdout, p.stdout)
    # SessionStart builds the probe index in the background; let it land before
    # the copy is compared, so a late write inside it cannot slip past.
    wait_for(state / "probe-index.json", state / "probe-index.json.error")
    check("the background index build wrote to ~/.recall",
          (state / "probe-index.json").exists() or (state / "probe-index.json.error").exists())

    seed = (state / "catalogs" / "DECISIONS.yaml")
    seed.write_text("- id: kept\n")
    run_hook("SessionStart", root, env)
    check("a second SessionStart never overwrites a catalog", seed.read_text() == "- id: kept\n")

    (state / "pending" / "s0.json").write_text(json.dumps({"session_id": "s0"}))
    p = run_hook("SessionStart", root, env)
    check("the plugin names its namespaced command", "Run /recall:compound" in p.stdout, p.stdout)
    (state / "pending" / "s0.json").unlink()

    # SessionEnd: a substantive session is queued, one that already ran the
    # command is not -- in either the plain or the plugin's namespaced form.
    for sid, extra, queued in (("s1", "", True),
                               ("s2", "<command-name>/recall:compound</command-name>", False),
                               ("s3", "<command-name>/compound</command-name>", False)):
        t = transcript(home / f"{sid}.jsonl", extra)
        p = run_hook("SessionEnd", root, env, {"session_id": sid, "transcript_path": str(t)})
        check(f"SessionEnd {sid} exits 0", p.returncode == 0, p.stderr)
        check(f"SessionEnd {sid} {'queues' if queued else 'skips'} the session",
              (state / "pending" / f"{sid}.json").exists() == queued)
    t = home / "s4.jsonl"
    transcript(t)
    with t.open("a") as fh:
        fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "/recall:compound s1"}}) + "\n")
    run_hook("SessionEnd", root, env, {"session_id": "s4", "transcript_path": str(t)})
    check("SessionEnd skips a typed /recall:compound", not (state / "pending" / "s4.json").exists())

    # The injector, on both events it is registered for.
    idx = home / "idx.json"
    idx.write_text(json.dumps({
        "postings": {TOKEN: ["FM::sandbox"]}, "weights": {TOKEN: 5.0},
        "specific_tokens": [TOKEN],
        "entries": {"FM::sandbox": {"catalog": "FM", "id": "sandbox-flag-entry",
                                    "summary": "what happened", "fix": "what to do"}}}))
    ienv = dict(env)
    ienv["RECALL_PROBE_INDEX"] = str(idx)
    p = run_hook("UserPromptSubmit", root, ienv, {"hook_event_name": "UserPromptSubmit",
                 "session_id": "i1", "prompt": f"the {TOKEN} bit us"})
    check("UserPromptSubmit injects from the copy", "sandbox-flag-entry" in p.stdout, p.stdout + p.stderr)
    p = run_hook("PostToolUse", root, ienv, {"hook_event_name": "PostToolUse", "session_id": "i2",
                 "tool_name": "Write", "tool_input": {"file_path": "a.html", "content": TOKEN}})
    check("PostToolUse injects from the copy", "sandbox-flag-entry" in p.stdout, p.stdout + p.stderr)
    check("the injector logs to ~/.recall", (state / "surfaced.jsonl").is_file())

    p = run_hook("PreCompact", root, env, {"session_id": "c1", "trigger": "manual",
                                           "hook_event_name": "PreCompact"})
    check("PreCompact runs from the copy", p.returncode == 0, p.stderr)
    check("PreCompact writes its record under ~/.recall", (state / "compaction-log.jsonl").is_file())

    # A host that exports the variable but does not write it into the string.
    home2 = base / "home2"
    home2.mkdir()
    p = run_hook("SessionStart", root, clean_env(home2), substitute=False)
    check("SessionStart also runs when the root is only in the environment",
          p.returncode == 0 and (home2 / ".recall" / "catalogs" / "DECISIONS.yaml").is_file(), p.stderr)
    wait_for(home2 / ".recall" / "probe-index.json", home2 / ".recall" / "probe-index.json.error")

    # Double registration, seen from the running hooks.
    home3 = base / "home3"
    install_settings_hooks(home3, "/Users/someone/recall")
    env3 = clean_env(home3)
    p = run_hook("SessionStart", root, env3)
    lines = [ln for ln in p.stdout.splitlines() if "runs twice" in ln]
    check("the plugin says once that install.sh hooks also run", len(lines) == 1, p.stdout)
    check("the line names the uninstall command",
          bool(lines) and "/Users/someone/recall/install.sh --uninstall" in lines[0], lines)
    p = subprocess.run(["bash", str(REPO / "hooks" / "recall-sessionstart.sh")], input="{}",
                       capture_output=True, text=True, env=env3, timeout=60)
    check("the install.sh copy stays quiet, so the line is not printed twice",
          "runs twice" not in p.stdout, p.stdout)
    (home3 / ".recall" / "pending").mkdir(parents=True, exist_ok=True)
    (home3 / ".recall" / "pending" / "s9.json").write_text(json.dumps({"session_id": "s9"}))
    p = subprocess.run(["bash", str(REPO / "hooks" / "recall-sessionstart.sh")], input="{}",
                       capture_output=True, text=True, env=env3, timeout=60)
    check("the install.sh copy names the plain command", "Run /compound to process" in p.stdout, p.stdout)
    wait_for(home3 / ".recall" / "probe-index.json", home3 / ".recall" / "probe-index.json.error")

    after = snapshot(root)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    check("nothing was written inside the plugin copy", not changed, changed[:8])
    check("no bytecode cache inside the plugin copy", not list(root.rglob("__pycache__")))
    # control: the comparison must see a write, or its silence proves nothing
    (root / "scripts" / "stray.json").write_text("{}")
    check("control: a write inside the copy is detected", snapshot(root) != before)
finally:
    shutil.rmtree(base, ignore_errors=True)


# ------------------------------------------------- install.sh refuses to double --
def run_install(home: Path, *args: str, extra: dict | None = None) -> subprocess.CompletedProcess:
    env = clean_env(home)
    env["PATH"] = f"{home / 'bin'}:{env['PATH']}"
    env["RECALL_HOME"] = str(home / "state")
    env.update(extra or {})
    return subprocess.run(["sh", "-c", f"yes y | '{REPO}/install.sh' " + " ".join(args)],
                          capture_output=True, text=True, env=env, cwd=REPO, timeout=60)


def guard_case(label: str, *, installed=None, enabled=None, args=("--host", "claude"),
               refused: bool, plugins_root: str | None = None) -> None:
    home = Path(tempfile.mkdtemp())
    try:
        (home / "bin").mkdir()
        shim = home / "bin" / "launchctl"           # never touch the real launchd
        shim.write_text("#!/bin/sh\nexit 0\n")
        shim.chmod(0o755)
        (home / ".claude").mkdir()
        (home / ".codex").mkdir()
        (home / ".codex" / "hooks.json").write_text('{"hooks":{}}')
        settings = {"enabledPlugins": enabled} if enabled else {}
        (home / ".claude" / "settings.json").write_text(json.dumps(settings))
        extra = {}
        if installed:
            proot = home / (plugins_root or ".claude/plugins")
            proot.mkdir(parents=True)
            (proot / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
                pid: [{"scope": "user", "installPath": "/x", "version": "0123456789ab"}]
                for pid in installed}}))
            if plugins_root:
                extra["CLAUDE_CODE_PLUGIN_CACHE_DIR"] = str(proot)
        p = run_install(home, *args, extra=extra)
        if refused:
            check(f"[{label}] install.sh refuses", p.returncode != 0, p.stdout[-200:])
            check(f"[{label}] the refusal names the plugin id", "recall@" in p.stderr, p.stderr)
            check(f"[{label}] nothing was changed",
                  json.loads((home / ".claude" / "settings.json").read_text()) == settings
                  and not (home / "state").exists()
                  and not (home / ".claude" / "skills").exists())
        else:
            check(f"[{label}] install.sh proceeds", p.returncode == 0, p.stderr)
    finally:
        shutil.rmtree(home, ignore_errors=True)


guard_case("plugin installed", installed=["recall@ok-wow"], refused=True)
guard_case("plugin enabled", enabled={"recall@ok-wow": True}, refused=True)
guard_case("dry run is refused too", installed=["recall@ok-wow"],
           args=("--host", "claude", "--dry-run"), refused=True)
guard_case("plugins folder moved by CLAUDE_CODE_PLUGIN_CACHE_DIR", installed=["recall@ok-wow"],
           plugins_root="elsewhere/plugins", refused=True)
guard_case("plugin disabled and not installed", enabled={"recall@ok-wow": False}, refused=False)
guard_case("another plugin whose name only starts with recall",
           installed=["recall-extra@someone"], refused=False)
guard_case("Codex host is not a Claude plugin", installed=["recall@ok-wow"],
           args=("--host", "codex"), refused=False)
guard_case("uninstall always runs, so people can leave install.sh for the plugin",
           installed=["recall@ok-wow"], args=("--host", "claude", "--uninstall"), refused=False)

if fails:
    print(f"FAIL {len(fails)} of {ran[0]} check(s):")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASS {ran[0]}/{ran[0]} plugin runtime checks (hooks from a cached copy, both double-install guards)")
