#!/usr/bin/env python3
"""Parked work, as recall sees it -- recall.py load_lot, show, --lot, --stats.

A parked item nobody can find again is the failure this feature exists to end,
so the load-bearing case is the first one: park an item, then ask recall a
question in plain words that shares no phrase with the title, and get it back.

Two other things are guarded here. A parked item is a plan, not a lesson, so it
must never reach the probe index that auto-injects lessons. And every suite
that runs recall or park must pin RECALL_LOT_DIR, or its fixture quietly reads
the owner's real lot -- the same leak test_recall.py documents for specs.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(os.environ.get("RECALL_SKILL_DIR") or REPO / "scripts")
RECALL = SCRIPT_DIR / "recall.py"
PARK = SCRIPT_DIR / "park.py"
BUILDER = SCRIPT_DIR / "build_probe_index.py"

FM = [{"id": "worktree-node-modules-symlink",
       "what": "A worktree node_modules symlink resolves workspace packages to the donor tree.",
       "fix_pattern": "Copy node_modules instead of symlinking.",
       "probe_when": ["`npm ci` inside a git worktree"], "recurrences": 0}]


def env_for(d: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("RECALL_", "OKWOW_"))}
    # Pin every corpus recall can read, not only the lot: an unpinned source
    # defaults to a real path under $HOME and silently enlarges the fixture.
    env.update({"HOME": str(d / "home"),
                "RECALL_HOME": str(d / "recall-home"),
                "RECALL_CATALOG_DIR": str(d / "catalogs"),
                "RECALL_LOT_DIR": str(d / "lot"),
                "RECALL_SPECS_DIR": str(d / "no-specs"),
                "RECALL_SKILLS_DIR": str(d / "no-skills"),
                "RECALL_SPECS_INDEX": str(d / "no-specs.txt"),
                "RECALL_HUB_INDEX": str(d / "no-hub.json"),
                "RECALL_CONNECTOR_DIR": str(d / "no-connectors"),
                "RECALL_RECEIPT_DIR": str(d / "no-receipts"),
                "RECALL_ORPHAN_INDEX": str(d / "no-orphans.yaml"),
                "RECALL_PROBE_INDEX": str(d / "recall-home" / "probe-index.json"),
                "RECALL_SURFACED_LOG": str(d / "surfaced.jsonl"),
                "RECALL_SESSION_ID": "sess-recall-test"})
    return env


def run(script: Path, d: Path, *args: str) -> tuple[int, str, str]:
    p = subprocess.run([sys.executable, str(script), *args],
                       capture_output=True, text=True, env=env_for(d))
    return p.returncode, p.stdout, p.stderr


def corpus() -> Path:
    import yaml
    d = Path(tempfile.mkdtemp(prefix="lotrecall-"))
    (d / "catalogs").mkdir()
    (d / "catalogs" / "FAILURE_MODES.yaml").write_text(yaml.safe_dump(FM, sort_keys=False))
    (d / "catalogs" / "PROCESS_FAILURES.yaml").write_text("[]\n")
    return d


# A suite reads the lot when it binds a module-level name to recall.py or
# park.py and then spawns a process, or when it loads recall's entries in its
# own process. Both shapes are matched; merely naming recall.py (as
# test_link_scripts does, as a file to link) is not.
BINDS = re.compile(r"^\w+\s*=.*[\"'](recall|park)\.py[\"']", re.M)
LOADS = re.compile(r"\.load_(entries|lot)\(")


def runs_the_lot_readers(text: str) -> bool:
    return bool((BINDS.search(text) and "subprocess" in text)
                or ("recall.py" in text and LOADS.search(text)))


# Stores whose default lives under $HOME and that a fixture must never read.
# The specs folder joined when recall started reading decision logs from it.
MUST_PIN = ("RECALL_LOT_DIR", "RECALL_SPECS_DIR")


def unpinned_suites(texts: dict[str, str]) -> list[str]:
    return sorted(n for n, t in texts.items()
                  if runs_the_lot_readers(t) and any(f'"{v}"' not in t for v in MUST_PIN))


def main() -> int:
    fails, ran = [], [0]

    def check(name, cond, detail=""):
        ran[0] += 1
        print(f"  [{'ok ' if cond else 'FAIL'}] {name}" + ("" if cond else f"  {detail}"))
        if not cond:
            fails.append(name)

    d = corpus()

    def add(*args: str) -> dict:
        rc, out, err = run(PARK, d, "add", *args, "--json")
        try:
            return json.loads(out)
        except Exception:
            return {"id": None, "error": f"rc={rc} {out[:120]} {err[:120]}"}

    slack = add("--title", "Slack connector sync should retry on rate limits",
                "--body", "The nightly sync dies on the first 429 and the whole batch is lost.",
                "--theme", "connectors", "--tier", "1", "--first-move", "wrap the fetch in exponential backoff",
                "--session", "s-origin", "--source-kind", "handoff",
                "--source-ref", "specs/_compaction/slack/v3.md", "--quote", "we should do this later")
    inbox = add("--title", "Tablet split view clips the onboarding footer",
                "--body", "At 768 wide in split view the continue button sits under the fold.")
    check("the fixture items were parked", bool(slack.get("id")) and bool(inbox.get("id")),
          f"{slack} {inbox}")

    # THE case: plain words, no phrase shared with the title.
    rc, out, err = run(RECALL, d, "--json", "slack keeps hitting the 429 limit overnight")
    try:
        rows = json.loads(out)
    except Exception:
        rows = []
    check("a plain question finds a parked item through recall's own search",
          any(r["id"] == slack["id"] and r["catalog"] == "LOT" for r in rows), f"rc={rc} {out[:200]} {err[:200]}")

    rc, out, err = run(RECALL, d, "slack keeps hitting the 429 limit overnight")
    block = out[out.find(slack["id"]):] if slack["id"] in out else ""
    check("the hit is tagged [LOT]", "[LOT]" in block, out[:300])
    check("the hit shows its tier", "tier 1" in block and "do next" in block, block[:300])
    check("the hit shows status and theme", "parked" in block and "connectors" in block, block[:300])
    check("the hit shows where it came from",
          "s-origin" in block and "specs/_compaction/slack/v3.md" in block, block[:300])
    check("the hit shows the first move", "FIRST MOVE: wrap the fetch" in block, block[:300])

    rc, out, err = run(RECALL, d, "--id", inbox["id"])
    check("an unsorted item says so", rc == 0 and "not sorted" in out, out[:300])

    # --lot is park.py list, not a second copy of it.
    rc, lot_text, err = run(RECALL, d, "--lot")
    rc2, list_text, _ = run(PARK, d, "list")
    check("recall --lot prints exactly what park.py list prints",
          rc == 0 and rc2 == 0 and lot_text == list_text and slack["id"] in lot_text,
          f"\n--lot:\n{lot_text}\n--list:\n{list_text}")
    rc, out, err = run(RECALL, d, "--lot", "--json")
    try:
        ids = [i["id"] for i in json.loads(out)]
    except Exception:
        ids = out[:200]
    check("recall --lot --json lists open items in park.py's order", ids == [slack["id"], inbox["id"]], str(ids))
    rc, out, err = run(RECALL, d, "--lot", "--json", "node_modules worktree symlink")
    try:
        cats = {r["catalog"] for r in json.loads(out)}
    except Exception:
        cats = {out[:120]}
    check("recall --lot with a question searches only the lot", cats <= {"LOT"}, str(cats))

    # --stats: counted by catalog, and never scored as an injection gap.
    rc, out, err = run(RECALL, d, "--stats", "--json")
    try:
        s = json.loads(out)
    except Exception:
        s = {}
    check("--stats counts LOT in by_catalog", s.get("by_catalog", {}).get("LOT") == 2, str(s.get("by_catalog")))
    check("--stats does not count parked items as lessons injection cannot reach",
          s.get("catalog_entries") == len(FM) and s.get("unreachable_by_injection") == len(FM),
          f"catalog={s.get('catalog_entries')} unreachable={s.get('unreachable_by_injection')}")
    for flag in ("--unreachable", "--stubs"):
        rc, out, err = run(RECALL, d, flag, "--json")
        try:
            cats = {r["catalog"] for r in json.loads(out)}
        except Exception:
            cats = {"unparsed"}
        check(f"{flag} is about lessons and leaves the lot out", "LOT" not in cats, str(cats))

    # A parked item is a plan. Auto-injection is for lessons.
    iframe = add("--title", "Add `allow-same-origin` to the preview `iframe` sandbox",
        "--body", "The `sandbox` attribute on the preview `iframe` needs `allow-same-origin`.")
    p = subprocess.run([sys.executable, str(BUILDER)], capture_output=True, text=True, env=env_for(d))
    try:
        keys = set(json.loads((d / "recall-home" / "probe-index.json").read_text())["entries"])
    except Exception as exc:
        keys = {f"unreadable: {exc} {p.stderr[:200]}"}
    check("the probe index builds over the same home", any(k.startswith("FM:") for k in keys), str(keys)[:200])
    check("no parked item ever enters the probe index", not any(k.startswith("LOT:") for k in keys),
          str(sorted(keys))[:200])

    # Done and killed leave the index; the rest of the lot stays.
    rc, _, _ = run(PARK, d, "done", slack["id"])
    rc, out, err = run(RECALL, d, "--json", "slack keeps hitting the 429 limit overnight")
    check("a done item leaves recall's index", slack["id"] not in out, out[:200])
    rc, _, _ = run(PARK, d, "kill", inbox["id"], "--why", "fixed by the new footer")
    rc, out, err = run(RECALL, d, "--json", "tablet split view onboarding footer")
    check("a killed item leaves recall's index", inbox["id"] not in out, out[:200])
    rc, out, err = run(RECALL, d, "--lot", "--json")
    check("closed items leave --lot", slack["id"] not in out and inbox["id"] not in out, out[:200])

    # One corrupt file costs one item, not the lot.
    keep = add("--title", "Deck export drops custom fonts", "--body", "Fonts fall back to Arial in PDF export.")
    (d / "lot" / "half-written.json").write_text('{"id": "half-written", "title": "cut off')
    rc, out, err = run(RECALL, d, "--json", "deck export fonts fall back in the pdf")
    check("a corrupt item file is reported by name", "half-written.json" in err and "LOT" in err, err[:300])
    check("the rest of the lot still loads past it", keep.get("id") in out, out[:200])
    rc, out, err = run(PARK, d, "list")
    check("park.py list survives it too and says so",
          rc == 0 and keep.get("id") in out and "half-written.json" in err, f"rc={rc} {err[:200]}")
    os.environ["RECALL_LOT_DIR"] = str(d / "lot")
    spec = importlib.util.spec_from_file_location("recall_lot_under_test", RECALL)
    recall = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recall)
    broken: list[dict] = []
    loaded = recall.load_lot(broken)
    check("load_lot reports the bad file through broken, like every loader",
          len(broken) == 1 and broken[0]["catalog"] == "LOT", str(broken))
    check("load_lot indexes only open items",
          {e["id"] for e in loaded} == {keep.get("id"), iframe.get("id")}, str([e["id"] for e in loaded]))

    # -- every suite that runs recall or park pins the lot ---------------------
    here = Path(__file__).resolve().parent
    texts = {t.name: t.read_text() for t in here.glob("test_*.py")}
    missing = unpinned_suites(texts)
    check("every suite that runs recall.py or park.py pins the lot and the specs folder",
          not missing, str(missing))
    check("the scan sees the suites it has to guard",
          {"test_recall.py", "test_park.py", "test_lot_in_recall.py"}
          <= {n for n, t in texts.items() if runs_the_lot_readers(t)})
    # A check that cannot fail proves nothing.
    unpinned = ('RECALL = SCRIPT_DIR / "recall.py"\n'
                'p = subprocess.run([sys.executable, str(RECALL), "q"], env=env)\n')
    check("control: an unpinned suite is caught", unpinned_suites({"x.py": unpinned}) == ["x.py"])
    check("control: a suite that pins only one of them is caught",
          unpinned_suites({"x.py": unpinned + '"RECALL_LOT_DIR": str(d)\n'}) == ["x.py"])
    check("control: a fully pinned suite passes",
          unpinned_suites({"x.py": unpinned + '"RECALL_LOT_DIR": str(d)\n"RECALL_SPECS_DIR": str(d)\n'}) == [])
    check("control: naming recall.py without running it is out of scope",
          unpinned_suites({"x.py": '(d / "recall.py").write_text("x")\nsubprocess.run(["bash"])\n'}) == [])

    print(f"\n{'FAIL' if fails else 'PASS'} {ran[0] - len(fails)}/{ran[0]} lot-in-recall checks")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
