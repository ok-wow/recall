#!/usr/bin/env python3
"""Precision/recall fixtures for the probe injector.

Run after ANY change to the matcher or the index builder:
    python3 tests/test_probe_inject.py

Every negative fixture here is a real false positive this hook actually
produced, not an invented one:
  - "status/completed/background" fired against a background-task notification
    on the hook's first live fire (length-based specificity).
  - "refresh/notify/follow" scored 19.5 because the catalog spells them as the
    button labels `Refresh`, `Notify`, `Follow`, and a LEADING capital was
    being read as camelCase.
A matcher that fires on ordinary English teaches the reader to skip the
channel, and then the one hit that mattered is invisible too. Precision is the
whole product here.

The catalog those fixtures match against is built by this file, in a temp
RECALL_HOME, by the real index builder -- see the FM/PF corpus below. A fresh
clone ships no catalogs, so a suite that read the machine's index had no
positive case at all and still reported 18 of 25 green.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import sys
import uuid
from pathlib import Path

# Per-entry-per-session dedupe is a FEATURE of the hook, and it silently turned
# a second run of this suite into 7 phantom failures. Fixtures must not share
# session ids with any previous run.
RUN = uuid.uuid4().hex[:8]

HOME = Path(os.environ.get("RECALL_HOME") or Path.home() / ".recall")
REPO = Path(__file__).resolve().parent.parent

# The hook core is invoked directly, not through its shell wrapper, so the
# suite tests the matcher rather than the wrapper's dependency guards.
_SKILL_DIR = os.environ.get("RECALL_SKILL_DIR")
if _SKILL_DIR:
    HOOK = Path(_SKILL_DIR) / "recall-probe-inject.py"
elif (REPO / "scripts" / "recall-probe-inject.py").exists():
    HOOK = REPO / "scripts" / "recall-probe-inject.py"
else:
    HOOK = REPO / "hooks" / "recall-probe-inject.py"

# (should_fire, text)
PROMPTS = [
    (False, "refresh the widget and notify me, then follow up"),
    (False, "continue and return early from that loop"),
    (False, "captured the change, now merge it"),
    (False, "whats the status of the background job, did it complete?"),
    (False, "summarize this PR for me"),
    (False, "fix the typo in the readme"),
    (False, "the background color feels off, make it lighter"),
    (False, "[SYSTEM NOTIFICATION] <task-notification><status>completed</status>"
            "<summary>Background command completed (exit code 0)</summary></task-notification>"),
    (False, "the output file is /tmp/agent-tasks/b2rr4qtb1.output"),
    (False, "check ~/notes/specs/ for the handoff"),
    (True, "run gh pr merge --delete-branch on the stacked PR"),
    (True, 'call create_artifact(type="design_board") for the board'),
    (True, "the iframe needs allow-same-origin to load the preview"),
    (True, "add table=True to the SQLModel class"),
    (True, "use getComputedStyle to check the overflow"),
    (True, "add dark:bg-gray-800 to the card"),
]

EDITS = [
    (False, "README.md", "the"),
    (True, "src/Preview.tsx", 'sandbox="allow-scripts allow-same-origin"'),
]


# --- the corpus the fixtures above are matched against -----------------------
# The injector matches a prompt against a prebuilt index, and a fresh clone ships
# no catalogs, so there is no index and nothing can fire. Every "should fire"
# case was silently untestable while every "should be silent" case kept passing
# -- 18/25 green with the precision-vs-recall point of the suite entirely gone.
# So the suite builds its own catalog in a temp RECALL_HOME and runs the real
# index builder over it. Same discipline as test_recall.py: the data the
# assertions need is created by the test, never borrowed from the machine.
#
# None of these entries are filler. Each is either the entry a positive fixture
# expects to hit, or the entry whose tokens produced a negative fixture's real
# false positive:
#   `Refresh`/`Notify`/`Follow`        leading capital read as camelCase
#   `status`/`completed`/`background`  length used as a proxy for specificity
#   `/tmp/`, `~/projects/specs`, `specs/`  path furniture that idf rates as rare
#   `README.md`                        one short punctuated token carrying a hit alone
# Without them the negatives pass because the catalog holds nothing that could
# ever match, which is not the same claim as the matcher rejecting them.
FM = [
    # Entries the positive fixtures expect to hit.
    {"id": "iframe-sandbox-drops-allow-same-origin",
     "summary": "A sandboxed iframe could not read its own document: the sandbox attribute "
                "granted scripts but not same-origin access.",
     "fix_pattern": "Grant allow-same-origin alongside allow-scripts, or stop reading the frame's document.",
     "probe_when": ["a `sandbox` attribute on an `iframe` that must read its own document",
                    "`allow-scripts` granted without `allow-same-origin`"],
     "recurrences": 2},
    {"id": "sqlmodel-class-without-table-true",
     "summary": "A SQLModel subclass declared without table=True created no table, and the first "
                "insert failed at runtime instead of at import.",
     "fix_pattern": "Add table=True to any SQLModel class that owns rows.",
     "probe_when": ["declaring a `SQLModel` subclass that owns rows",
                    "`table=True` missing from a model definition"],
     "recurrences": 1},
    {"id": "getcomputedstyle-forces-sync-layout",
     "summary": "getComputedStyle inside a scroll handler forced a synchronous layout every frame.",
     "fix_pattern": "Read layout once outside the handler and cache it.",
     "probe_when": ["`getComputedStyle` inside a scroll or resize handler",
                    "reading `overflow` or `offsetHeight` mid-animation"],
     "recurrences": 0},
    {"id": "tailwind-dark-variant-never-compiled",
     "summary": "A dark variant in a file outside the Tailwind content globs was never compiled, so "
                "the class existed in the markup and nowhere in the stylesheet.",
     "fix_pattern": "Add the file to the content globs, or move the class into a scanned file.",
     "probe_when": ["adding `dark:bg-gray-800` to a component",
                    "any `dark:bg-` variant in a file outside the content globs"],
     "recurrences": 3},
    {"id": "gh-pr-merge-delete-branch-orphans-the-stack",
     "summary": "Merging the base of a stacked PR with --delete-branch retargeted the child at main "
                "and dragged the parent's commits back into its diff.",
     "fix_pattern": "Retarget the child PR first, then merge the base.",
     "probe_when": ["`gh pr merge` on a PR that has another branch stacked on it",
                    "`--delete-branch` while a child branch is still open"],
     "recurrences": 2},
    {"id": "create-artifact-design-board-created-empty",
     "summary": "create_artifact returned a design_board with no seed content, and an empty board is "
                "indistinguishable from a failed call.",
     "fix_pattern": "Seed the board in the same call that creates it.",
     "probe_when": ["`create_artifact` called with no seed content",
                    "a `design_board` that renders empty on first open"],
     "recurrences": 0},

    # Entries whose tokens produced the negative fixtures' real false positives.
    {"id": "refresh-notify-follow-share-one-toolbar-handler",
     "summary": "Three toolbar buttons shared a handler and dispatched the wrong action.",
     "fix_pattern": "One handler per labelled action.",
     "probe_when": ["wiring the `Refresh`, `Notify` and `Follow` buttons in the toolbar"],
     "recurrences": 0},
    {"id": "background-job-status-completed-before-writes-land",
     "summary": "A background job reported completion before its writes were visible to the next "
                "reader, so the consumer read a half-written file.",
     "fix_pattern": "Report completion after the flush, not after the exit code.",
     "probe_when": ["a `background` job whose `status` reads `completed` before its writes land",
                    "a `summary` line trusted as a durability signal"],
     "recurrences": 1},
    {"id": "scratch-paths-baked-into-committed-fixtures",
     "summary": "A committed fixture pointed at a machine-local scratch path and passed only on the "
                "machine that wrote it.",
     "fix_pattern": "Build the path from a temp dir inside the test.",
     "probe_when": ["a fixture writing under `/tmp/`",
                    "a path like `~/projects/specs` baked into a committed test",
                    "`specs/` reached by a relative path from a hook",
                    "`git status --porcelain` showing untracked test output"],
     "recurrences": 1},
    {"id": "readme-only-edit-runs-the-full-ci-matrix",
     "summary": "A one-word docs edit ran the whole matrix for forty minutes.",
     "fix_pattern": "Add a paths-ignore for docs-only changes.",
     # Two tokens deliberately: `readme` alone carries the score past the floor so the
     # README.md edit is rejected by the rule that case is about — one short punctuated
     # token cannot carry a hit by itself — and not merely by arriving under-weight.
     "probe_when": ["editing `README.md` alone on a branch with a heavy CI matrix",
                    "a `readme`-only commit that still fans out the matrix"],
     "recurrences": 0},
    {"id": "early-return-skips-the-cleanup-block",
     "summary": "An early return inside a loop that held a lock skipped the release.",
     "fix_pattern": "Release in a finally block, not on the success path.",
     "probe_when": ["a `continue` inside a loop that holds a lock",
                    "a `return early` path that bypasses cleanup"],
     "recurrences": 0},
    {"id": "captured-work-merged-without-a-rebase",
     "summary": "Work captured on a long-lived branch was merged without rebasing and reintroduced a "
                "file main had already deleted.",
     "fix_pattern": "Rebase onto main before the merge.",
     "probe_when": ["work `captured` on a long-lived branch and then `merge`d without a rebase"],
     "recurrences": 0},
]

PF = [
    {"id": "probe-tokens-written-as-prose-never-fire",
     "summary": "An entry whose probe_when was plain prose was dropped from the index entirely: the "
                "lesson existed and could never surface.",
     "fix_pattern": "Give every entry at least one backticked literal.",
     "probe_when": ["editing `probe_when` on a catalog entry",
                    "adding an entry to `FAILURE_MODES.yaml` with no backticked token"],
     "recurrences": 4},
    {"id": "generic-lone-token-fires-on-every-session",
     "summary": "Ordinary developer vocabulary used as a sole trigger fired on most sessions, and "
                "the reader learned to skip the channel.",
     "fix_pattern": "A lone trigger must identify a situation; pair generic words with a literal.",
     "probe_when": ["a probe token as generic as `worktree` or `origin/main`",
                    "`pre-commit` or `services/` standing alone as a trigger"],
     "recurrences": 2},
    {"id": "test-fixtures-written-into-the-production-signal-log",
     "summary": "The suite drove the hook as a subprocess with inherited env, and 170 of 378 rows in "
                "the signal log turned out to be fixtures.",
     "fix_pattern": "Point the log and the dedupe store at a temp dir for every fixture call.",
     "probe_when": ["a suite that drives `recall-probe-inject.py` as a subprocess",
                    "`surfaced.jsonl` growing during a test run"],
     "recurrences": 1},
]

if _SKILL_DIR:
    BUILDER = Path(_SKILL_DIR) / "build_probe_index.py"
else:
    BUILDER = REPO / "scripts" / "build_probe_index.py"


# The injector's MIN_SCORE is an ABSOLUTE floor and the weights are idf over the
# corpus, so corpus SIZE decides which rule is actually operative. With 15
# entries a unique token scores log(15)=2.71, BELOW the 3.0 floor — no single
# token can ever fire, the floor silently does all the work, and every negative
# fixture written to prove a SHAPE rule passes for the wrong reason. Verified by
# deletion: with 15 entries you could remove the whole path-furniture mechanism,
# or the self-referential filter, or every stoptoken, and this suite stayed green.
#
# Break-even is e^3.0 = 20.09. Padding to 22+ puts a unique token at log(22)=3.09,
# clearing the floor exactly as production's 1377 entries do (log = 7.23), which
# hands the decision back to the shape rules these fixtures exist to test.
FILLER_COUNT = 10


def filler_entries():
    """Entries that exist only to make idf realistic. See FILLER_COUNT."""
    return [
        {
            "id": f"filler-entry-{i}",
            "what": f"Filler entry {i}. Present only so the corpus is large enough that a "
                    f"single token clears MIN_SCORE, as it does in production.",
            "fix_pattern": "Not applicable.",
            "probe_when": [f"`filler-token-{i}-unique` appears"],
            "recurrences": 0,
        }
        for i in range(FILLER_COUNT)
    ]


def build_probe_index() -> Path:
    """Write the synthetic catalog and run the REAL builder over it.

    RECALL_HOME is the documented seam for all mutable state and the injector
    reads $RECALL_HOME/probe-index.json, so a temp home is enough to keep the
    real ~/.recall untouched — including its index, which a rebuild pointed
    anywhere else would have overwritten with these fixture entries.

    The builder is run rather than hand-writing an index, because half of what
    the negative fixtures assert (path furniture dropped, leading capitals not
    treated as camelCase, stoptokens) is decided in the builder. A hand-written
    index would assert the matcher while quietly exempting the builder.
    """
    import yaml

    home = Path(tempfile.mkdtemp(prefix=f"recall-probe-home-{RUN}-"))
    catalogs = home / "catalogs"
    catalogs.mkdir()
    (catalogs / "FAILURE_MODES.yaml").write_text(
        yaml.safe_dump(FM + filler_entries(), sort_keys=False))
    (catalogs / "PROCESS_FAILURES.yaml").write_text(yaml.safe_dump(PF, sort_keys=False))
    p = subprocess.run(
        [sys.executable, str(BUILDER)], capture_output=True, text=True,
        env={**os.environ, "RECALL_HOME": str(home),
             "RECALL_CATALOG_DIR": str(catalogs)},
    )
    if p.returncode != 0:
        raise SystemExit(f"could not build the test probe index with {BUILDER}:\n"
                         f"{(p.stderr or p.stdout).strip()}")
    return home


TEST_HOME = build_probe_index()

# Fixtures must not land in the production signal log. That log is the only
# evidence of whether retrieval works, and for months this suite wrote into it
# -- 170 of 378 rows -- so every metric derived from it moved when the tests ran.
TEST_LOG = Path(tempfile.gettempdir()) / f"recall-probe-surfaced-test-{RUN}.jsonl"
TEST_STATE = Path(tempfile.gettempdir()) / f"recall-probe-state-test-{RUN}"
TEST_ENV = {**os.environ,
            "RECALL_HOME": str(TEST_HOME),
            "RECALL_PROBE_SURFACED_LOG": str(TEST_LOG),
            "RECALL_PROBE_STATE_DIR": str(TEST_STATE)}
PROD_STATE = HOME / "probe-state"


def fire(payload: dict) -> list[str]:
    out = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload),
        capture_output=True, text=True, env=TEST_ENV,
    ).stdout.strip()
    if not out:
        return []
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    return [l for l in ctx.splitlines() if l.startswith(("[FM]", "[PF]"))]


PROD_LOG = HOME / "surfaced.jsonl"


def prod_log_size() -> int:
    try:
        return PROD_LOG.stat().st_size
    except FileNotFoundError:
        return -1


def main() -> int:
    failures = []
    # Setup, not a fixture: if the catalog did not survive indexing, every
    # positive case below fails for a reason that has nothing to do with the
    # matcher. That is precisely the failure this suite was in — seven dead
    # "should fire" cases and no statement anywhere that the corpus was empty.
    index = json.loads((TEST_HOME / "probe-index.json").read_text())
    expected = len(FM) + len(PF) + FILLER_COUNT   # derived, never hardcoded
    if len(index["entries"]) != expected:
        print(f"SETUP FAILED: indexed {len(index['entries'])} of {expected} fixture entries "
              f"({TEST_HOME / 'probe-index.json'}). An entry lost every usable probe token, "
              "so the cases that depend on it cannot mean anything.")
        return 1

    # The isolation above is only real while every call passes TEST_ENV, and the
    # next fixture someone adds will not. Assert the outcome, not the intent:
    # 172 of 380 rows in the production signal log were once this suite's output.
    prod_before = prod_log_size()
    state_before = len(list(PROD_STATE.glob("*.json"))) if PROD_STATE.is_dir() else -1
    for i, (want, text) in enumerate(PROMPTS):
        got = bool(fire({"hook_event_name": "UserPromptSubmit", "prompt": text,
                         "session_id": f"regress-{RUN}-p{i}"}))
        if got != want:
            failures.append(f"prompt {'should fire' if want else 'should be silent'}: {text[:60]!r}")

    for i, (want, path, content) in enumerate(EDITS):
        got = bool(fire({"hook_event_name": "PostToolUse", "tool_name": "Write",
                         "session_id": f"regress-{RUN}-e{i}",
                         "tool_input": {"file_path": path, "content": content}}))
        if got != want:
            failures.append(f"edit {'should fire' if want else 'should be silent'}: {path}")

    # A Read must never be considered at all.
    if fire({"hook_event_name": "PostToolUse", "tool_name": "Read", "session_id": f"regress-{RUN}-read",
             "tool_input": {"file_path": "src/Preview.tsx"}}):
        failures.append("Read tool was not ignored")

    # Malformed input must stay silent and exit 0.
    for bad in ("not json", "{}", "[]", '{"hook_event_name":"UserPromptSubmit"}'):
        r = subprocess.run([sys.executable, str(HOOK)], input=bad,
                           capture_output=True, text=True, env=TEST_ENV)
        if r.returncode != 0 or r.stdout.strip():
            failures.append(f"malformed input not handled: {bad[:30]!r}")

    if prod_log_size() != prod_before:
        failures.append(
            "THIS SUITE WROTE TO THE PRODUCTION SIGNAL LOG "
            f"({PROD_LOG}) — a fixture is missing env=TEST_ENV. "
            "That log is the only evidence retrieval works; test rows make every "
            "metric derived from it a function of how often the tests ran.")

    state_after = len(list(PROD_STATE.glob("*.json"))) if PROD_STATE.is_dir() else -1
    if state_after != state_before:
        failures.append(
            f"THIS SUITE WROTE TO THE PRODUCTION DEDUPE STORE ({PROD_STATE}) — "
            f"{state_before} -> {state_after} session files. A fixture is missing "
            "env=TEST_ENV, and test state can suppress a real session's injections.")

    total = len(PROMPTS) + len(EDITS) + 7
    if failures:
        print(f"FAIL {len(failures)}/{total}")
        for f in failures:
            print("  -", f)
        return 1
    print(f"PASS {total}/{total} fixtures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
