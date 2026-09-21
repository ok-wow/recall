#!/usr/bin/env python3
"""Fixtures for recall.py --hidden — statements no channel will ever display.

A recurrence note records the SAME requirement failing again. When someone
files a NEW or widened requirement there instead, it is written down and
undeliverable: push renders summarize()/remedy(), pull renders show()'s
fields, and no `recurrence_*` key is in either list. BM25 still indexes the
words, so the statement ranks in search and never appears on screen. In the
corpus this check was written for, a correction recorded on the day it was
given was absent from the line the agent reads, and the same mistake repeated
six days later.

The load-bearing pair is `a hidden statement is reported` and `the same
statement promoted into a displayed field is not`. Same words, same note,
opposite verdicts — a check that only ever fires in one direction is not
measuring the thing it claims to measure.

Runs against a synthetic catalog in a temp dir (RECALL_CATALOG_DIR), never the
real one, and redirects the surfaced log, so the suite cannot perturb
production state or be perturbed by it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(os.environ.get("RECALL_SKILL_DIR")
                  or Path(__file__).resolve().parent.parent / "scripts")
RECALL = SCRIPT_DIR / "recall.py"

# The one statement under test, reused verbatim so every case differs only in
# WHERE it is written. Its content words are absent from ordinary entry prose,
# which is the point: a correction usually introduces vocabulary the entry does
# not already have, and that absence is exactly what the check measures.
SAID = "ask a person for approval, never a review bot"

FM = [
    # 1. The failure this mode exists for: the requirement is on file, and the
    #    only field carrying it is one nothing renders.
    {"id": "a-correction-only-in-a-recurrence-note",
     "what": "A branch was merged while the owner was away from the machine.",
     "fix_pattern": "Wait for the owner to say go.",
     "probe_when": ["a merge nobody has looked at"],
     "recurrences": 1,
     "recurrence_2026_09_15": f'The owner corrected this again: "{SAID}"'},

    # 2. Same words, same note, and this time also in a field show() prints.
    #    Reported here would mean the check cannot tell delivered from buried.
    {"id": "the-same-correction-promoted-into-the-body",
     "what": f"A merge proceeded on a bot's approval. {SAID.capitalize()}.",
     "fix_pattern": "Wait for the owner to say go.",
     "recurrences": 1,
     "recurrence_2026_09_15": f'The owner corrected this again: "{SAID}"'},

    # 3. Under 20 characters is a label, not a requirement. The quote carries
    #    real content words that no displayed field holds, so it is the length
    #    rule alone that keeps this entry out of the report — drop the
    #    threshold and this fixture is reported.
    {"id": "a-short-quote-is-not-a-requirement",
     "what": "A queue marker was deleted by the worker that wrote it.",
     "fix_pattern": "Let the system observe progress.",
     "recurrences": 2,
     "recurrence_2026_09_16": 'Second time, and the note says "ask the owner" and nothing more.'},

    # 4. Quoted prose outside a recurrence note is ordinary entry content.
    {"id": "an-entry-with-no-recurrence-note",
     "what": f'A reviewer wrote "{SAID}" in a comment and the comment was lost.',
     "fix_pattern": "Put the requirement where the tool reads it."},
]

PF = [
    # 5. The single-source-of-truth control. `context` is read by the PUSH
    #    channel's summarize() and by nothing in show(). If --hidden compared
    #    against a pasted copy of show()'s field list, this would be reported,
    #    and the report would be wrong: push prints it.
    {"id": "carried-only-by-the-push-channels-field",
     "what": "A release went out with no changelog entry.",
     "context": f"The team agreed: {SAID}, before any release.",
     "fix": "Write the changelog first.",
     "recurrences": 1,
     "recurrence_2026_09_16": f'Repeated, and the owner restated it: "{SAID}"'},

    # 6. The other reason this is derived from the code and not from a list of
    #    names: with no recognised body field, show() falls back to the longest
    #    unknown string — which here IS the note. It is on screen, so it is not
    #    hidden, and only calling the display code can know that.
    {"id": "the-note-is-the-only-body-so-it-is-on-screen",
     "probe_when": ["a merge proposed by an agent"],
     "recurrences": 1,
     "recurrence_2026_09_17": f'The owner said it once more, in full: "{SAID} on any pull request"'},
]


def catalog_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="recallhidden-"))
    import yaml
    (d / "FAILURE_MODES.yaml").write_text(yaml.safe_dump(FM, sort_keys=False))
    (d / "PROCESS_FAILURES.yaml").write_text(yaml.safe_dump(PF, sort_keys=False))
    return d


def run(d: Path, *args: str, surfaced_log: str | None = None) -> tuple[int, str]:
    # RECALL_SURFACED_LOG defaults INTO the temp dir so a caller cannot forget
    # it. recall.py logs need-driven pulls, and a fixture id appended to the
    # real retrieval log is not a cosmetic leak: it is evidence, and this
    # project has already published a baseline that was 45% its own fixtures.
    env = {**os.environ, "RECALL_CATALOG_DIR": str(d),
           # Pin the last unpinned path too, or every fixture corpus silently
           # gains the real rules from ~/.claude/skills.
           "RECALL_SKILLS_DIR": str(Path(__file__).resolve().parent / "fixtures" / "no-skills"),
           "RECALL_PROBE_INDEX": str(d / "nonexistent-index.json"),
           "RECALL_SURFACED_LOG": surfaced_log or str(d / "surfaced.jsonl")}
    p = subprocess.run([sys.executable, str(RECALL), *args],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    d = catalog_dir()
    fails: list[str] = []
    ran = [0]

    def check(name, cond, detail=""):
        ran[0] += 1
        if cond:
            print(f"  [ok ] {name}")
        else:
            fails.append(f"{name}: {detail}")
            print(f"  [FAIL] {name}  {detail}")

    rc, out = run(d, "--hidden", "--json")
    try:
        found = json.loads(out)
    except Exception as e:
        print(f"  [FAIL] --hidden --json parses: {e}\n{out[:400]}")
        return 1
    ids = [f["id"] for f in found]

    check("--hidden reports a statement no channel displays",
          ids.count("a-correction-only-in-a-recurrence-note") == 1, f"ids={ids}")
    check("a statement a displayed field carries is not reported",
          "the-same-correction-promoted-into-the-body" not in ids, f"ids={ids}")
    check("a quote under 20 characters is ignored",
          "a-short-quote-is-not-a-requirement" not in ids, f"ids={ids}")
    check("an entry with no recurrence note is ignored",
          "an-entry-with-no-recurrence-note" not in ids, f"ids={ids}")
    check("a statement the PUSH channel displays is not reported",
          "carried-only-by-the-push-channels-field" not in ids,
          "summarize() reads `context`; show() does not — the audit must ask both")
    check("a note that IS the displayed body is not reported",
          "the-note-is-the-only-body-so-it-is-on-screen" not in ids,
          "show() falls back to the longest unknown field, which here is the note")
    check("nothing else is reported", len(found) == 1, f"{len(found)} findings: {ids}")

    row = found[0] if found else {}
    check("a finding names the entry, the key, the words and what is missing",
          {"id", "catalog", "key", "statement", "missing"} <= set(row), repr(row)[:160])
    check("the finding points at the unrendered key",
          row.get("key") == "recurrence_2026_09_15", row.get("key"))
    check("the finding quotes what was said",
          "review bot" in str(row.get("statement", "")), row.get("statement"))
    check("missing words are a list, not a rendered string",
          isinstance(row.get("missing"), list) and "approval" in row.get("missing", []),
          repr(row.get("missing")))

    # Exit code follows the other report modes: 0 whether or not it found
    # something. A corpus is not a file, and whether a backlog blocks anything
    # is the owner's policy.
    check("--hidden exits 0 with findings", rc == 0, f"rc={rc}")
    rc, out = run(d, "--hidden")
    check("human output leads with the count and the entry",
          rc == 0 and "1 statement" in out and "a-correction-only-in-a-recurrence-note" in out,
          out[:160])

    rc, out = run(d, "--stats", "--json")
    try:
        s = json.loads(out)
        check("--stats carries the count", s.get("hidden_statements") == len(found),
              f"stats={s.get('hidden_statements')} audit={len(found)}")
    except Exception as e:
        check("--stats emits valid json", False, str(e))

    # A corpus with nothing to report must say so and still exit 0 — the same
    # empty-is-not-broken rule the rest of this tool holds.
    _ed = d / "empty-corpus"
    _ed.mkdir(exist_ok=True)
    for _c in ("FAILURE_MODES", "PROCESS_FAILURES", "DECISIONS"):
        (_ed / f"{_c}.yaml").write_text("[]\n")
    rc, out = run(_ed, "--hidden")
    check("empty corpus: --hidden succeeds and says nothing is hidden",
          rc == 0 and "no statement" in out, f"rc={rc} {out[:90]}")
    rc, out = run(_ed, "--hidden", "--json")
    check("empty corpus: --hidden --json is an empty list", rc == 0 and json.loads(out) == [])

    # --hidden is a report ABOUT delivery, not a delivery. Logging it would
    # inflate the surfacing metric with the act of auditing surfacing — the
    # same reason --unreachable and --stats do not log.
    plog = d / "pull-probe.jsonl"

    def rows() -> list:
        if not plog.exists():
            return []
        return [json.loads(x) for x in plog.read_text().splitlines() if x.strip()]

    run(d, "--id", "a-correction-only-in-a-recurrence-note", surfaced_log=str(plog))
    before = len(rows())
    check("the control works — a pull IS logged", before >= 1, f"{before} rows")
    for flag in ("--hidden", "--unreachable"):
        run(d, flag, surfaced_log=str(plog))
    check("auditing is NOT a surface", len(rows()) == before, f"{before} -> {len(rows())}")

    # The override must be HONOURED, not merely available.
    env = {**os.environ, "RECALL_CATALOG_DIR": str(d),
           # Pin the last unpinned path too, or every fixture corpus silently
           # gains the real rules from ~/.claude/skills.
           "RECALL_SKILLS_DIR": str(Path(__file__).resolve().parent / "fixtures" / "no-skills"),
           "RECALL_SURFACED_LOG": str(plog), "RECALL_HOME": str(d / "fake-home")}
    subprocess.run([sys.executable, str(RECALL), "--hidden"],
                   capture_output=True, text=True, env=env)
    check("an audit never touches production state",
          not (d / "fake-home").exists() or not (d / "fake-home" / "surfaced.jsonl").exists())

    if fails:
        print(f"\nFAIL {len(fails)}/{ran[0]}")
        for f in fails:
            print("  -", f)
        return 1
    print(f"\nPASS {ran[0]}/{ran[0]} hidden-statement fixtures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
