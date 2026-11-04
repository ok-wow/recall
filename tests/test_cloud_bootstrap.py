#!/usr/bin/env python3
"""scripts/cloud_bootstrap.sh: set Recall up inside an ephemeral cloud container, safely, twice."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import REPO, check, clean, env_for, finish, run, tmp  # noqa: E402

BOOT = REPO / "scripts" / "cloud_bootstrap.sh"
SECRET = "gh" + "p_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0hJ3kL6"


def boot(home: Path, **extra):
    e = env_for(home, **extra)
    e.pop("RECALL_HOME", None)
    e.pop("RECALL_HOST_DIR", None)
    e.update({k: v for k, v in extra.items()})
    return run(["bash", str(BOOT)], e)


def commands(settings: dict) -> list:
    return [(ev, h["command"]) for ev, groups in settings.get("hooks", {}).items() for g in groups for h in g["hooks"]]


print("cloud_bootstrap")
h = tmp()
(h / ".claude").mkdir()
(h / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
state = h / "container-recall"
kw = dict(RECALL_BOOTSTRAP_MODE="hooks", RECALL_HOME=str(state), GH_TOKEN=SECRET)
p1 = boot(h, **kw)
s1 = json.loads((h / ".claude" / "settings.json").read_text())
want = set(json.loads((REPO / "hooks" / "hooks.json").read_text())["hooks"])
check("hooks mode exits 0", p1.returncode == 0, p1.stdout + p1.stderr)
check("hooks mode registers every hook the plugin manifest declares, pointing into this checkout",
      set(s1["hooks"]) == want and all(str(REPO) in c for _, c in commands(s1)), str(sorted(s1.get("hooks", {}))))
check("the friction hook is on PostToolUse and Stop; the probe injector is still on PostToolUse",
      any("recall-friction" in c for e, c in commands(s1) if e == "Stop")
      and any("recall-friction" in c for e, c in commands(s1) if e == "PostToolUse")
      and any("recall-probe-inject" in c for e, c in commands(s1) if e == "PostToolUse"), str(commands(s1)))
check("RECALL_HOME is a container path and sync is switched on, via settings env",
      s1.get("env", {}).get("RECALL_HOME") == str(state) and s1["env"].get("RECALL_SYNC") == "1", str(s1.get("env")))
check("existing settings are preserved", s1.get("permissions") == {"allow": ["Bash(ls)"]})
check("state directories exist", all((state / d).is_dir() for d in ("pending", "inbox", "outbox")))
check("a credential in the environment is never written to settings or printed",
      SECRET not in (h / ".claude" / "settings.json").read_text() and SECRET not in p1.stdout + p1.stderr)
check("it says what sync still needs (a remote), instead of staying quiet", "RECALL_PERSONAL_REMOTE" in p1.stdout, p1.stdout)

p2 = boot(h, **kw)
s2 = json.loads((h / ".claude" / "settings.json").read_text())
check("safe to run twice: identical settings, no duplicate hooks", p2.returncode == 0 and s2 == s1, str(commands(s2)))
check("...and the second run says nothing changed", "already" in p2.stdout, p2.stdout)

# plugin route: the documented commands, through a stand-in claude CLI
h2 = tmp()
(h2 / "bin").mkdir()
log = h2 / "claude.log"
shim = h2 / "bin" / "claude"
shim.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\nexit 0\n")
shim.chmod(0o755)
pk = dict(RECALL_BOOTSTRAP_MODE="plugin", RECALL_HOME=str(h2 / "rc"), PATH=f"{h2 / 'bin'}:{os.environ['PATH']}")
p = boot(h2, **pk)
calls = log.read_text().splitlines() if log.exists() else []
check("plugin mode runs the two documented commands",
      "plugin marketplace add ok-wow/recall" in calls and "plugin install recall@ok-wow" in calls, str(calls))
check("plugin mode still points the plugin at the container's RECALL_HOME and turns sync on",
      json.loads((h2 / ".claude" / "settings.json").read_text()).get("env", {}).get("RECALL_SYNC") == "1")
check("plugin mode does not also register hooks (they would run twice)",
      not json.loads((h2 / ".claude" / "settings.json").read_text()).get("hooks"))
check("plugin mode twice is fine", boot(h2, **pk).returncode == 0)

shim.write_text("#!/bin/sh\nexit 1\n")
h3 = tmp()
(h3 / "bin").mkdir()
sh3 = h3 / "bin" / "claude"
sh3.write_text("#!/bin/sh\nexit 1\n")
sh3.chmod(0o755)
p = boot(h3, RECALL_BOOTSTRAP_MODE="plugin", RECALL_HOME=str(h3 / "rc"), PATH=f"{h3 / 'bin'}:{os.environ['PATH']}")
s3 = json.loads((h3 / ".claude" / "settings.json").read_text()) if (h3 / ".claude" / "settings.json").exists() else {}
check("a failing plugin install falls back to hooks, says so, and still exits 0",
      p.returncode == 0 and "falling back" in p.stdout + p.stderr and s3.get("hooks"), p.stdout + p.stderr)
clean(h, h2, h3)
sys.exit(finish("cloud_bootstrap checks"))
