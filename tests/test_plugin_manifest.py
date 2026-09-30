#!/usr/bin/env python3
"""The plugin manifests and hooks/hooks.json must match what install.sh wires.

Recall installs two ways into Claude Code: install.sh writes hooks into
settings.json, and the plugin carries the same hooks in hooks/hooks.json. Two
lists of the same wiring drift, so the truth for the plugin is taken from the
REAL installer, run into a throwaway HOME: every event it registers must be in
hooks.json with the same script and timeout, and nothing else may be.

Rules checked, each from the Claude Code plugin docs:
  - plugin.json needs only `name`. It must NOT carry `version`: a fixed version
    keeps every user on the cached copy until someone bumps it by hand, while no
    version makes each commit a new version.
  - the marketplace names one plugin, with the same name, at source "./".
  - hooks.json commands are `bash "<root>/hooks/x.sh"` or `python3 "<root>/x.py"`,
    so the cache copy needs no executable bit, and the placeholder is quoted.
  - no `args`. Exec form would also avoid the executable bit, but Codex reads
    Claude-format plugins and documents no `args` field. A host that ignores
    `args` would run a bare `bash` with the hook's JSON on stdin, and bash
    expands `$(...)` inside that JSON's strings -- a prompt or an edited file
    would become a command. Shell form runs the same on both hosts.
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
PLACEHOLDER = "${CLAUDE_PLUGIN_ROOT}"
CMD_RE = re.compile(r'^(bash|python3) "\$\{CLAUDE_PLUGIN_ROOT\}/([^"$]+)"$')
INTERPRETER = {".sh": "bash", ".py": "python3"}

fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        fails.append(f"{label}{(' — ' + detail) if detail else ''}")


def has_version(d: dict) -> bool:
    return "version" in d or "version" in (d.get("metadata") or {})


def plugin_problems(d: dict) -> list[str]:
    out = []
    if not isinstance(d.get("name"), str) or not d["name"]:
        out.append("plugin.json has no name")
    if not isinstance((d.get("author") or {}).get("name"), str):
        out.append("plugin.json author has no name")
    if not d.get("description"):
        out.append("plugin.json has no description")
    if has_version(d):
        out.append("plugin.json pins a version")
    return out


def market_problems(d: dict, plugin_name: str) -> list[str]:
    out = []
    for key in ("name", "owner", "plugins"):
        if key not in d:
            out.append(f"marketplace.json has no {key}")
    if not (d.get("owner") or {}).get("name"):
        out.append("marketplace owner has no name")
    if has_version(d):
        out.append("marketplace.json pins a version")
    entries = d.get("plugins") or []
    if len(entries) != 1:
        return out + [f"marketplace lists {len(entries)} plugins, expected 1"]
    e = entries[0]
    if e.get("name") != plugin_name:
        out.append(f"marketplace entry is {e.get('name')!r}, plugin.json is {plugin_name!r}")
    if e.get("source") != "./":
        out.append(f"marketplace source is {e.get('source')!r}, expected './'")
    if "version" in e:
        out.append("marketplace entry pins a version")
    return out


def hook_wiring(d: dict, root: Path) -> tuple[dict, list[str]]:
    """{event: (script path relative to the plugin root, timeout)} plus problems."""
    wiring, out = {}, []
    for event, groups in (d.get("hooks") or {}).items():
        for g in groups or []:
            for h in g.get("hooks") or []:
                cmd = h.get("command", "")
                if "args" in h:
                    out.append(f"{event}: uses args (exec form)")
                m = CMD_RE.match(cmd)
                if not m:
                    out.append(f"{event}: command not in the form interpreter \"{PLACEHOLDER}/path\": {cmd}")
                    continue
                interp, rel = m.groups()
                if INTERPRETER.get(Path(rel).suffix) != interp:
                    out.append(f"{event}: {rel} is run with {interp}")
                if not (root / rel).is_file():
                    out.append(f"{event}: {rel} does not exist in the repo")
                wiring[event] = (rel, h.get("timeout"))
    return wiring, out


def installer_wiring() -> dict:
    """What install.sh actually registers for Claude Code, read back from disk."""
    home = Path(tempfile.mkdtemp())
    try:
        (home / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_text("{}")
        (home / "bin").mkdir()
        shim = home / "bin" / "launchctl"          # never touch the real launchd
        shim.write_text("#!/bin/sh\nexit 0\n")
        shim.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_PLUGIN_CACHE_DIR"}
        env.update(HOME=str(home), PATH=f"{home / 'bin'}:{env['PATH']}",
                   RECALL_HOME=str(home / "state"))
        p = subprocess.run(["sh", "-c", f"yes y | '{REPO}/install.sh' --host claude"],
                           capture_output=True, text=True, env=env, cwd=REPO)
        check("install.sh runs in a throwaway HOME", p.returncode == 0, p.stderr[-300:])
        d = json.loads((home / ".claude" / "settings.json").read_text())
        out = {}
        for event, groups in (d.get("hooks") or {}).items():
            for g in groups:
                for h in g.get("hooks", []):
                    out[event] = (os.path.relpath(h["command"], REPO), h.get("timeout"))
        return out
    finally:
        shutil.rmtree(home, ignore_errors=True)


def wiring_problems(plugin: dict, installer: dict) -> list[str]:
    out = []
    for ev in sorted(set(installer) - set(plugin)):
        out.append(f"install.sh wires {ev}, hooks.json does not")
    for ev in sorted(set(plugin) - set(installer)):
        out.append(f"hooks.json wires {ev}, install.sh does not")
    for ev in sorted(set(plugin) & set(installer)):
        if plugin[ev] != installer[ev]:
            out.append(f"{ev}: hooks.json runs {plugin[ev]}, install.sh runs {installer[ev]}")
    return out


def load(rel: str) -> dict:
    try:
        return json.loads((REPO / rel).read_text())
    except Exception as e:                    # a missing file is the finding
        check(f"{rel} is valid JSON", False, str(e))
        return {}


# ---------------------------------------------------------------- real checks --
plugin = load(".claude-plugin/plugin.json")
market = load(".claude-plugin/marketplace.json")
hooks = load("hooks/hooks.json")

for p in plugin_problems(plugin):
    check(p, False)
check("plugin is named recall", plugin.get("name") == "recall", str(plugin.get("name")))
for p in market_problems(market, plugin.get("name", "")):
    check(p, False)
check("marketplace is named ok-wow, so the install id is recall@ok-wow",
      market.get("name") == "ok-wow", str(market.get("name")))

got, problems = hook_wiring(hooks, REPO)
for p in problems:
    check(p, False)
installed = installer_wiring()
check("install.sh registered hooks to compare against", bool(installed))
for p in wiring_problems(got, installed):
    check(p, False)

# -- negative controls: a check that cannot fail proves nothing --------------
check("control: a pinned version is caught",
      "plugin.json pins a version" in plugin_problems({**plugin, "version": "1.0.0"}))
check("control: a renamed marketplace entry is caught",
      any("expected" in p or "plugin.json is" in p for p in
          market_problems({**market, "plugins": [{"name": "other", "source": "./"}]}, "recall")))
_bad = {"hooks": {"SessionEnd": [{"hooks": [
    {"type": "command", "command": f'bash "{PLACEHOLDER}/hooks/not-shipped.sh"'},
    {"type": "command", "command": "bash", "args": [f"{PLACEHOLDER}/hooks/x.sh"]},
    {"type": "command", "command": f"bash {PLACEHOLDER}/hooks/recall-sessionend.sh"},
    {"type": "command", "command": f'python3 "{PLACEHOLDER}/hooks/recall-sessionend.sh"'},
]}]}}
_, _bad_problems = hook_wiring(_bad, REPO)
check("control: a missing script is caught", any("does not exist" in p for p in _bad_problems))
check("control: exec form is caught", any("uses args" in p for p in _bad_problems))
check("control: an unquoted placeholder is caught", any("not in the form" in p for p in _bad_problems))
check("control: the wrong interpreter is caught", any("is run with python3" in p for p in _bad_problems))
check("control: a missing event is caught",
      wiring_problems({}, {"PreCompact": ("hooks/x.py", 10)}) == ["install.sh wires PreCompact, hooks.json does not"])
check("control: a different timeout is caught",
      bool(wiring_problems({"SessionEnd": ("hooks/a.sh", 5)}, {"SessionEnd": ("hooks/a.sh", 10)})))

TOTAL = 6 + len(installed) + 7
if fails:
    print(f"FAIL {len(fails)} check(s):")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASS {TOTAL}/{TOTAL} plugin manifest checks ({len(installed)} events match install.sh, 7 controls)")
