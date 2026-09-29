#!/usr/bin/env python3
"""One /compound skill file must find its scripts in every install.

The same SKILL.md is served two ways: install.sh links it into the agent's
skills folder, and the plugin carries it inside a cached copy of the repo.
Claude Code writes the skill's own folder into `${CLAUDE_SKILL_DIR}` for both,
while `${CLAUDE_PLUGIN_ROOT}` is filled in for plugin skills only. So the skill
reaches its scripts through `${RECALL_SKILL_DIR:-${CLAUDE_SKILL_DIR}/../../scripts}`:
the drain's explicit setting first, then two folders up from the skill.

This substitutes the skill the way Claude Code does for each install, runs the
resulting path through bash, and requires it to reach a scripts folder that
actually runs recall.py.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "compound" / "SKILL.md"
EXPR = "${RECALL_SKILL_DIR:-${CLAUDE_SKILL_DIR}/../../scripts}"
CMD_RE = re.compile(r'python3 "([^"]+)/([A-Za-z0-9_]+\.py)"')

fails: list[str] = []
ran = [0]


def check(label: str, ok: bool, detail: str = "") -> None:
    ran[0] += 1
    if not ok:
        fails.append(f"{label}{(' — ' + str(detail)[-300:]) if detail else ''}")


def fenced(text: str) -> list[str]:
    return re.findall(r"```[a-z]*\n(.*?)```", text, re.S)


def resolve(expr: str, env: dict) -> Path:
    """Expand a path the way the Bash tool would, then follow links."""
    p = subprocess.run(["bash", "-c", f'printf %s "{expr}"'], capture_output=True, text=True, env=env)
    return Path(os.path.realpath(p.stdout)) if p.stdout else Path("/nonexistent")


def base_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RECALL_", "OKWOW_", "CLAUDE_"))}
    env["HOME"] = str(home)
    env["RECALL_HOME"] = str(home / ".recall")
    return env


body = SKILL.read_text()
commands = [c for block in fenced(body) for c in CMD_RE.findall(block)]
check("the skill runs its scripts through python3", len(commands) >= 4, commands)
for expr, script in commands:
    check(f"{script} is reached through the shared expression", expr == EXPR, expr)
    check(f"{script} is shipped in scripts/", (REPO / "scripts" / script).is_file())
check("the skill never uses ${CLAUDE_PLUGIN_ROOT}, which a linked skill never gets",
      "${CLAUDE_PLUGIN_ROOT}" not in body)
check("the skill no longer uses the bare $RECALL_SKILL_DIR/ form",
      '"$RECALL_SKILL_DIR/' not in body)

base = Path(tempfile.mkdtemp())
try:
    home = base / "home"
    (home / ".recall" / "catalogs").mkdir(parents=True)
    for c in ("FAILURE_MODES", "PROCESS_FAILURES", "DECISIONS"):
        (home / ".recall" / "catalogs" / f"{c}.yaml").write_text("[]\n")

    # install.sh: a link in the agent's skills folder pointing into the clone.
    link = home / ".claude" / "skills" / "compound"
    link.parent.mkdir(parents=True)
    link.symlink_to(REPO / "skills" / "compound")
    # the plugin: a copy of the repo in a versioned cache folder.
    copy = base / "plugins" / "cache" / "ok-wow" / "recall" / "0123456789ab"
    shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(".git", "__pycache__"))

    cases = [
        # (label, what Claude Code writes into ${CLAUDE_SKILL_DIR}, RECALL_SKILL_DIR, expected)
        ("install.sh link, path as linked", str(link), None, REPO / "scripts"),
        ("install.sh link, path as resolved", str(link.resolve()), None, REPO / "scripts"),
        ("plugin copy", str(copy / "skills" / "compound"), None, copy / "scripts"),
        ("drain, which sets RECALL_SKILL_DIR", str(link), str(REPO / "scripts"), REPO / "scripts"),
    ]
    for label, skill_dir, override, want in cases:
        served = body.replace("${CLAUDE_SKILL_DIR}", skill_dir)     # as Claude Code loads it
        env = base_env(home)
        if override:
            env["RECALL_SKILL_DIR"] = override
        exprs = {e for block in fenced(served) for e, _ in CMD_RE.findall(block)}
        check(f"[{label}] one expression after substitution", len(exprs) == 1, exprs)
        for e in exprs:
            got = resolve(e, env)
            check(f"[{label}] resolves to {want}", got == want.resolve(), got)
            p = subprocess.run(["python3", f"{got}/recall.py", "--stats"],
                               capture_output=True, text=True, env=env, timeout=60)
            check(f"[{label}] recall.py runs from there", p.returncode == 0, p.stderr)

    # -- negative controls: the check above must be able to fail -------------
    env = base_env(home)
    check("control: a host that does not fill in the skill folder resolves nowhere",
          not (resolve(EXPR, env) / "recall.py").is_file())
    check("control: ${CLAUDE_PLUGIN_ROOT} in a linked skill resolves nowhere",
          not (resolve("${CLAUDE_PLUGIN_ROOT}/scripts", env) / "recall.py").is_file())
finally:
    shutil.rmtree(base, ignore_errors=True)

if fails:
    print(f"FAIL {len(fails)} of {ran[0]} check(s):")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASS {ran[0]}/{ran[0]} skill-path checks (install.sh link, plugin copy, drain; 2 controls)")
