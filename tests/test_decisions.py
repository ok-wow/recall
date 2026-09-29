#!/usr/bin/env python3
"""Decisions inside specs -- recall.py load_decisions.

Recall indexed one line per spec, so a decision written inside a spec's
decision log could not be found by the words it was written in. On
2026-09-29 the owner's own phrase, "artificially constrain on desktop and
tablet", sat in a dated decision in responsive-artifacts/spec.md and a query
for it did not return the spec. Decisions "felt unrecorded".

The corpus writes decisions two ways (a "Decision log" section of bullets, and
a front-matter decision_log: list), and the specs checkout often sits on a
long-lived branch whose working tree lacks files that are on origin/main. So
this suite builds a small specs repo in a temp dir with both formats and a
branch that has drifted from its origin/main, and asks recall for decisions by
phrases from inside them.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(os.environ.get("RECALL_SKILL_DIR") or REPO / "scripts")
RECALL = SCRIPT_DIR / "recall.py"
BUILDER = SCRIPT_DIR / "build_probe_index.py"

ALPHA = """---
title: Alpha spec
status: approved
---
# Alpha

## Context
General notes about desktop and tablet layouts.

## Decision log

- **2026-09-24 — Artifacts fill the panel (owner's call).** Owner: "we artificially constrain
  on desktop and tablet breakpoints". Only tiny widgets stay contained.
  - a nested note that belongs to the decision above
- **2026-06-12 · BRAINSTORM · DECIDED · Cards get a shell.** Apps go edge to edge.
- A decision with no date and no bold lead about the zebra crossing.
1. **2026-06-13 — Numbered decision about quokka habitats.**
{okapi}
### Decisions taken in review (2026-06-14)
- **Nested under a sub-heading, still in the log.** It is about wombats.

## Next steps
- **2026-06-15 — Outside the log.** Mentions a platypus.
"""
OKAPI = "- **2026-07-01 — Okapi decision that only origin/main has.** Kept on main."

BETA = """---
title: Beta spec
decision_log:
  - date: 2026-05-07T00:00:00Z
    what: "Use llms.txt over a custom JSON manifest"
    why: "The file already exists and reads well; a custom format is one more thing to teach."
    alternatives: ["custom JSON manifest", "GraphQL endpoint"]
    source: human
  - what: "An undated front matter decision about capybaras"
    why: "No date was written down."
---
# Beta
"""

GAMMA = """# Gamma

# Decisions

| Date | Decision | Why |
|---|---|---|
| 2026-05-13 | Shape is a life sim with an ocelot reveal | engagement beats utility |
"""

BROKEN = """---
title: Broken
decision_log: [unclosed
---
# Broken

## Decision log
- **2026-08-01 — The narwhal decision still loads.** The body log does not need the front matter.
"""

DELTA = """# Delta

## Decision log
- **2026-08-02 — Axolotl decision on origin/main only.** The branch deleted this spec.
- 2026-07-10 · EXECUTION · PR-S1 · **Test runner is vitest, not jest.** The plan said jest.
"""

ZETA = """# Zeta

## Decision log
- **2026-08-03 — Manatee decision in the working tree only.** Not committed anywhere.
"""

# A spec whose one-line summary shares only common words with the query.
LLMS = """# specs

## Approved
- [Epsilon spec](./epsilon-spec/spec.md): product · spec · approved · updated 2026-09-01 · Desktop and tablet layouts for the settings page ([preview](x))
"""

# The index as origin/main has it, and as the long-lived branch has it. Main
# gained theta after the branch was cut; each side holds the newer line of one
# spec; iota is the branch's own.
INDEX_LINE = "- [{t} spec](./{s}-spec/spec.md): product · spec · approved · updated {d} · {tldr} ([preview](x))\n"
LLMS_MAIN = (LLMS
             + INDEX_LINE.format(t="Theta", s="theta", d="2026-09-29", tldr="Pangolin baseline that only main lists")
             + INDEX_LINE.format(t="Kappa", s="kappa", d="2026-09-20", tldr="Heron summary as main has it")
             + INDEX_LINE.format(t="Lambda", s="lambda", d="2026-09-02", tldr="Egret summary as main has it"))
LLMS_BRANCH = (LLMS
               + INDEX_LINE.format(t="Kappa", s="kappa", d="2026-09-10", tldr="Heron summary as the branch has it")
               + INDEX_LINE.format(t="Lambda", s="lambda", d="2026-09-12", tldr="Egret summary as the branch has it")
               + INDEX_LINE.format(t="Iota", s="iota", d="2026-09-15", tldr="Tamarin summary that only the branch lists"))


# The fixture's git must not see the machine's git config: a global commit
# signing or hooks setting would fail or slow every commit here.
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(d: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                           "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                           "-C", str(d), *args], check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout


def build(root: Path) -> Path:
    specs = root / "specs"
    for slug, text in (("alpha-spec", ALPHA.replace("{okapi}", OKAPI)), ("beta-spec", BETA),
                       ("gamma-spec", GAMMA), ("broken-fm", BROKEN), ("delta-spec", DELTA)):
        (specs / slug).mkdir(parents=True)
        (specs / slug / "spec.md").write_text(text)
    (specs / "llms.txt").write_text(LLMS_MAIN)
    git(specs, "init", "-q", "-b", "main")
    git(specs, "add", "-A")
    git(specs, "commit", "-q", "-m", "main")
    git(specs, "update-ref", "refs/remotes/origin/main", "HEAD")
    # The long-lived branch: it deleted delta, dropped okapi, and has an
    # uncommitted spec of its own.
    git(specs, "checkout", "-q", "-b", "work")
    git(specs, "rm", "-q", "-r", "delta-spec")
    (specs / "alpha-spec" / "spec.md").write_text(ALPHA.replace("{okapi}", ""))
    (specs / "llms.txt").write_text(LLMS_BRANCH)
    git(specs, "commit", "-q", "-am", "work")
    (specs / "zeta-spec").mkdir()
    (specs / "zeta-spec" / "spec.md").write_text(ZETA)
    return specs


def env_for(root: Path, specs: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RECALL_", "OKWOW_"))}
    env.update({"HOME": str(root / "home"),
                "RECALL_HOME": str(root / "recall-home"),
                "RECALL_CATALOG_DIR": str(root / "catalogs"),
                "RECALL_SPECS_INDEX": str(specs / "llms.txt"),
                "RECALL_SPECS_DIR": str(specs),
                "RECALL_LOT_DIR": str(root / "no-lot"),
                "RECALL_SKILLS_DIR": str(root / "no-skills"),
                "RECALL_HUB_INDEX": str(root / "no-hub.json"),
                "RECALL_CONNECTOR_DIR": str(root / "no-connectors"),
                "RECALL_RECEIPT_DIR": str(root / "no-receipts"),
                "RECALL_ORPHAN_INDEX": str(root / "no-orphans.yaml"),
                "RECALL_PROBE_INDEX": str(root / "recall-home" / "probe-index.json"),
                "RECALL_SURFACED_LOG": str(root / "surfaced.jsonl")})
    return env


def main() -> int:
    fails, ran = [], [0]

    def check(name, cond, detail=""):
        ran[0] += 1
        print(f"  [{'ok ' if cond else 'FAIL'}] {name}" + ("" if cond else f"  {detail}"))
        if not cond:
            fails.append(name)

    root = Path(tempfile.mkdtemp(prefix="decisiontest-"))
    (root / "catalogs").mkdir()
    (root / "catalogs" / "FAILURE_MODES.yaml").write_text(
        "- id: fm-one\n  what: a lesson with a `backticked_token` in it\n"
        "  fix_pattern: do the thing\n  probe_when: ['`backticked_token` appears']\n")
    (root / "catalogs" / "PROCESS_FAILURES.yaml").write_text("[]\n")
    specs = build(root)
    env = env_for(root, specs)

    def run(*args: str) -> tuple[int, str, str]:
        p = subprocess.run([sys.executable, str(RECALL), *args], capture_output=True, text=True, env=env)
        return p.returncode, p.stdout, p.stderr

    # In-process, for the loader's own view.
    for k, v in env.items():
        if k.startswith("RECALL_"):
            os.environ[k] = v
    spec = importlib.util.spec_from_file_location("recall_decisions_under_test", RECALL)
    recall = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recall)
    broken: list[dict] = []
    got = recall.load_decisions(broken) if hasattr(recall, "load_decisions") else []
    by_spec: dict[str, list[dict]] = {}
    for e in got:
        by_spec.setdefault(e["raw"]["spec"], []).append(e)

    def find(word: str) -> dict:
        return next((e for e in got if word in e["text"]), {})

    # -- both formats ---------------------------------------------------------
    check("the section format parses: one decision per bullet, numbered item and sub-heading bullet",
          len(by_spec.get("alpha-spec", [])) == 6, [e["raw"]["title"] for e in by_spec.get("alpha-spec", [])])
    check("the front-matter format parses: one decision per list entry",
          len(by_spec.get("beta-spec", [])) == 2, [e["raw"]["title"] for e in by_spec.get("beta-spec", [])])
    fm = find("llms.txt")
    check("a front-matter decision carries its date, title and reasons",
          fm.get("raw", {}).get("date") == "2026-05-07"
          and fm["raw"]["title"] == "Use llms.txt over a custom JSON manifest"
          and "GraphQL endpoint" in fm["text"], str(fm.get("raw"))[:300])
    check("a front-matter decision points at the line it starts on",
          fm.get("raw", {}).get("line") == 4, str(fm.get("raw", {}).get("line")))
    check("a table row in a decision section is one decision",
          find("ocelot").get("raw", {}).get("date") == "2026-05-13"
          and find("ocelot")["raw"]["title"] == "Shape is a life sim with an ocelot reveal",
          str(find("ocelot").get("raw"))[:200])
    check("the header row of that table is not a decision", len(by_spec.get("gamma-spec", [])) == 1)
    check("--stats-style format counts: which specs used which format",
          getattr(recall.load_decisions, "formats", {}) == {"section": 5, "front_matter": 1},
          str(getattr(recall.load_decisions, "formats", None)))

    # -- shape of one decision ------------------------------------------------
    big = find("artificially constrain")
    r = big.get("raw", {})
    check("a bullet that continues on indented lines is one decision",
          "Only tiny widgets" in big.get("text", "") and "nested note" in big.get("text", ""), str(r)[:200])
    check("the bold lead is the title, without its date",
          r.get("title") == "Artifacts fill the panel (owner's call).", repr(r.get("title")))
    check("the date comes from the start of the lead", r.get("date") == "2026-09-24")
    check("the key names the spec, the date and the ordinal",
          big.get("key") == "DECISION:alpha-spec:2026-09-24:1", big.get("key"))
    check("the decision points at the file and the line it starts on",
          r.get("path") == str(specs / "alpha-spec" / "spec.md") and r.get("line") == 12,
          f"{r.get('path')}:{r.get('line')}")
    stage = find("Cards get a shell").get("raw", {})
    check("a status prefix like 'BRAINSTORM · DECIDED' is kept apart from the title",
          stage.get("title") == "Cards get a shell." and stage.get("stage") == "BRAINSTORM · DECIDED",
          str(stage)[:200])
    vt = find("vitest").get("raw", {})
    check("a date and stage written before the bold title are peeled off it too",
          (vt.get("date"), vt.get("stage"), vt.get("title"))
          == ("2026-07-10", "EXECUTION · PR-S1", "Test runner is vitest, not jest."), str(vt)[:200])
    check("a decision with no date is kept, with its date unknown",
          find("zebra crossing").get("raw", {}).get("date") is None
          and find("zebra").get("key", "").startswith("DECISION:alpha-spec:unknown:"),
          str(find("zebra").get("key")))
    check("a bullet under a dated sub-heading takes the sub-heading's date",
          find("wombats").get("raw", {}).get("date") == "2026-06-14", str(find("wombats").get("raw")))
    check("the section stops at the next heading of the same level", not find("platypus"))

    # -- where the specs are read from ----------------------------------------
    ax = find("Axolotl").get("raw", {})
    check("a spec that exists only on origin/main is found", ax.get("spec") == "delta-spec", str(ax)[:200])
    check("its location says it is on origin/main, not in this checkout",
          ax.get("path") == "origin/main:delta-spec/spec.md" and ax.get("line") == 4, str(ax)[:200])
    check("a decision only origin/main still has is found in a spec both have",
          find("Okapi").get("raw", {}).get("path") == "origin/main:alpha-spec/spec.md")
    check("a spec only in the working tree is found", find("Manatee").get("raw", {}).get("spec") == "zeta-spec")
    keys = [e["key"] for e in got]
    check("the union has no duplicates: a decision in both copies is indexed once",
          len(keys) == len(set(keys)) and sum("Numbered decision about quokka" in e["text"] for e in got) == 1,
          str(len(keys)))

    # -- the one-line index is read from both places too -----------------------
    sb: list[dict] = []
    lines = recall.load_specs(sb)
    idx = {e["id"]: e["raw"] for e in lines}
    here = lambda slug: str(specs / slug / "spec.md")
    check("a spec whose index line is only on origin/main is found",
          "Pangolin" in idx.get("theta-spec", {}).get("summary", ""), str(idx.get("theta-spec"))[:200])
    check("its path says it was read from origin/main",
          idx.get("theta-spec", {}).get("spec_path") == "origin/main:theta-spec/spec.md",
          str(idx.get("theta-spec", {}).get("spec_path")))
    check("a line only the branch's index has is kept",
          idx.get("iota-spec", {}).get("spec_path") == here("iota-spec"), str(idx.get("iota-spec"))[:200])
    check("a spec in both indexes is listed once",
          sorted(e["id"] for e in lines)
          == ["epsilon-spec", "iota-spec", "kappa-spec", "lambda-spec", "theta-spec"],
          str([e["id"] for e in lines]))
    check("when both list a spec, the later update wins: main's",
          "as main has it" in idx.get("kappa-spec", {}).get("summary", "")
          and idx["kappa-spec"]["spec_path"] == "origin/main:kappa-spec/spec.md", str(idx.get("kappa-spec"))[:200])
    check("when both list a spec, the later update wins: the branch's",
          "as the branch has it" in idx.get("lambda-spec", {}).get("summary", "")
          and idx["lambda-spec"]["spec_path"] == here("lambda-spec"), str(idx.get("lambda-spec"))[:200])
    check("a tie goes to the working tree",
          idx.get("epsilon-spec", {}).get("spec_path") == here("epsilon-spec"),
          str(idx.get("epsilon-spec", {}).get("spec_path")))
    check("reading both indexes reports nothing broken", not sb, str(sb))

    # -- a broken spec costs itself, not the corpus ----------------------------
    check("a broken front matter is reported through broken",
          any(b["catalog"] == "DECISION" and "broken-fm" in b["path"] for b in broken), str(broken))
    check("the same spec's body log still loads", bool(find("narwhal")))
    check("every other spec still loads", all(by_spec.get(s) for s in ("alpha-spec", "beta-spec", "gamma-spec")))

    # -- through recall's own search ------------------------------------------
    rc, out, err = run("--json", "-n", "5", "artificially constrain on desktop and tablet")
    try:
        rows = json.loads(out)
    except Exception:
        rows = []
    ids = [f"{r['catalog']}:{r['id']}" for r in rows]
    check("a phrase from inside a decision finds it first",
          ids[:1] == ["DECISION:alpha-spec:2026-09-24:1"], f"{ids} {err[:200]}")
    check("and ranks it above a spec whose summary only shares common words",
          "SPEC:epsilon-spec" not in ids
          or ids.index("SPEC:epsilon-spec") > ids.index("DECISION:alpha-spec:2026-09-24:1"), str(ids))
    rc, out, err = run("artificially constrain on desktop and tablet", "-n", "1")
    check("show prints the spec, the date, the title and path:line",
          "alpha-spec · 2026-09-24" in out and "Artifacts fill the panel" in out
          and f"{specs / 'alpha-spec' / 'spec.md'}:12" in out, out[:400])
    rc, out, err = run("axolotl decision", "-n", "1")
    check("show marks a decision that is only on origin/main", "origin/main:delta-spec/spec.md:4" in out, out[:300])
    rc, out, err = run("--specs", "pangolin baseline", "-n", "1")
    check("show marks a spec line that was read from origin/main, and says how to read the spec",
          "origin/main:theta-spec/spec.md" in out
          and f"git -C {specs} show origin/main:theta-spec/spec.md" in out, out[:400])
    rc, out, err = run("--specs", "--json", "capybaras undated")
    try:
        cats = [r["catalog"] for r in json.loads(out)]
    except Exception:
        cats = [out[:100]]
    check("--specs returns decisions, because asking for specs means asking what they decided",
          cats[:1] == ["DECISION"], str(cats))
    rc, out, err = run("--stats", "--json")
    try:
        s = json.loads(out)
    except Exception:
        s = {}
    check("--stats counts DECISION in by_catalog",
          s.get("by_catalog", {}).get("DECISION") == len(got) and len(got) > 0, str(s.get("by_catalog")))
    check("--stats reports which specs used which format",
          s.get("decision_formats") == {"section": 5, "front_matter": 1}, str(s.get("decision_formats")))
    check("--stats does not count decisions as lessons injection cannot reach",
          s.get("unreachable_by_injection") == 1, str(s.get("unreachable_by_injection")))
    rc, out, err = run("--id", "DECISION:alpha-spec:2026-09-24:1")
    check("--id finds a decision by its key", rc == 0 and "Artifacts fill the panel" in out, out[:200])

    # -- never pushed as a lesson ---------------------------------------------
    subprocess.run([sys.executable, str(BUILDER)], capture_output=True, text=True, env=env)
    try:
        idx = set(json.loads((root / "recall-home" / "probe-index.json").read_text())["entries"])
    except Exception as exc:
        idx = {f"unreadable {exc}"}
    check("the probe index builds", "FM:fm-one" in idx, str(idx)[:200])
    check("no decision ever enters the probe index", not any(k.startswith("DECISION:") for k in idx), str(idx)[:200])

    # -- recall only reads ----------------------------------------------------
    head = git(specs, "rev-parse", "--abbrev-ref", "HEAD").strip()
    status = git(specs, "status", "--porcelain")
    check("reading the specs repo leaves its branch and working tree as they were",
          head == "work" and status.strip() == "?? zeta-spec/", f"{head} {status!r}")
    no_git = root / "plain-specs"
    (no_git / "solo").mkdir(parents=True)
    (no_git / "solo" / "spec.md").write_text(ZETA.replace("Manatee", "Tapir"))
    os.environ["RECALL_SPECS_DIR"] = str(no_git)
    spec2 = importlib.util.spec_from_file_location("recall_decisions_plain", RECALL)
    plain = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(plain)
    b2: list[dict] = []
    tapir = plain.load_decisions(b2) if hasattr(plain, "load_decisions") else []
    check("a specs folder that is not a git repo is read from disk alone",
          [e["raw"]["title"] for e in tapir] == ["Tapir decision in the working tree only."] and not b2,
          f"{[e['raw']['title'] for e in tapir]} {b2}")

    (no_git / "llms.txt").write_text(LLMS)
    os.environ["RECALL_SPECS_INDEX"] = str(no_git / "llms.txt")
    spec3 = importlib.util.spec_from_file_location("recall_index_plain", RECALL)
    plain_idx = importlib.util.module_from_spec(spec3)
    spec3.loader.exec_module(plain_idx)
    b3: list[dict] = []
    check("an index that is not in a git repo is read from disk alone",
          [e["id"] for e in plain_idx.load_specs(b3)] == ["epsilon-spec"] and not b3, str(b3))
    # A repo that was never fetched has no origin/main to read.
    no_ref = root / "unfetched-specs"
    no_ref.mkdir()
    (no_ref / "llms.txt").write_text(LLMS)
    git(no_ref, "init", "-q", "-b", "main")
    os.environ["RECALL_SPECS_INDEX"] = str(no_ref / "llms.txt")
    os.environ["RECALL_SPECS_DIR"] = str(no_ref)
    spec4 = importlib.util.spec_from_file_location("recall_index_unfetched", RECALL)
    unfetched = importlib.util.module_from_spec(spec4)
    spec4.loader.exec_module(unfetched)
    b4: list[dict] = []
    check("a repo with no origin/main is read from disk alone, and nothing is reported broken",
          [e["id"] for e in unfetched.load_specs(b4)] == ["epsilon-spec"]
          and unfetched.load_decisions(b4) == [] and not b4, str(b4))

    print(f"\n{'FAIL' if fails else 'PASS'} {ran[0] - len(fails)}/{ran[0]} decision-loader checks")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
