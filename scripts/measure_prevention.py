#!/usr/bin/env python3
"""Did surfacing a lesson PREVENT the failure, or merely precede it?

Recall counts what it captured and counts what repeated. Neither answers the
only question that decides whether the loop earns its tokens: when a lesson
already in your memory came true AGAIN, had it been put in front of anyone
first? Those are two different failures and they need opposite fixes --

  SURFACED, then recurred  -> it was shown and it did not change the outcome.
                              More retrieval will not help. The entry's text,
                              its timing, or its enforcement has to change.
  NEVER SURFACED           -> the lesson existed and the system failed to
                              deliver it. Retrieval is exactly what helps.

Reported as one number they are indistinguishable, which is how "10% recurred
anyway" can sit in a README for months sounding like a verdict on memory when
it is mostly a verdict on delivery. Measured on the corpus this was extracted
from, 23 of 26 measurable recurrences had never surfaced at all.

    measure_prevention.py           human summary
    measure_prevention.py --json    machine-readable
    measure_prevention.py --gaps    only the entries that recurred unsurfaced

WHAT IT STILL CANNOT TELL YOU. Nothing here is causal. "Surfaced, then did not
recur" is not proof the lesson helped -- the situation may simply not have come
back. Only withholding a lesson from a random share of eligible moments and
comparing the two arms would settle that, and this tool does not do it. Read
`never-surfaced` as the actionable number and treat the rest as description.

HONESTY REQUIREMENT: this reports what it CANNOT measure as prominently as what
it can. A recurrence recorded as a bare counter has no date, so it cannot be
placed relative to a fire; a fire outside the log's window cannot be seen at
all. Both are printed as explicit unmeasurable buckets rather than quietly
dropped, because a denominator that shrinks to the convenient cases is the
failure this whole corpus exists to catch.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

def env_path(*names_then_default) -> Path:
    """First of several env vars that is set, else the default.

    Two names because this tool has two lineages that were merged: the shipped
    tool names its variables RECALL_*, and the okwow-compound install that has
    been running it names them OKWOW_*. Both sets of hooks and tests are still
    out there, so both are honoured, RECALL_ first.
    """
    *names, default = names_then_default
    for n in names:
        raw = os.environ.get(n)
        if raw:
            return Path(raw).expanduser()
    return Path(default).expanduser()


RECALL_HOME = env_path("RECALL_HOME", "OKWOW_HOME", Path.home() / ".recall")
CATALOG_DIR = env_path("RECALL_CATALOG_DIR", "OKWOW_CATALOG_DIR",
                       RECALL_HOME / "catalogs")
SURFACED_LOG = env_path("RECALL_SURFACED_LOG", "OKWOW_PROBE_SURFACED_LOG",
                        RECALL_HOME / "surfaced.jsonl")
PROBE_INDEX = env_path("RECALL_PROBE_INDEX", "OKWOW_PROBE_INDEX",
                       RECALL_HOME / "probe-index.json")
CATALOGS = {"FM": "FAILURE_MODES.yaml", "PF": "PROCESS_FAILURES.yaml",
            "DE": "DECISIONS.yaml"}

# A dated recurrence key is the only form that can be placed on a timeline.
DATED_KEY = re.compile(r"^recurrence[_-](\d{4})[_-](\d{2})[_-](\d{2})$")
# Test-harness rows are not evidence about production retrieval. recall.py's
# suite sets RECALL_SESSION_ID to a `test-` value for exactly this reason.
TEST_SESSION = re.compile(r"^(regress-|VISTEST|test-|probe-test|negctl-)", re.I)
# Both spellings occur. `recurrences: 3` is the common one; `recurrence: [...]`
# appears on older entries, and reading only the first silently drops them.
REC_FIELDS = ("recurrences", "recurrence")


def rec_count(e: dict) -> int:
    for k in REC_FIELDS:
        v = e.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            return v
        if isinstance(v, list):
            return len(v)
    return 0


def load_fires() -> dict[str, list[str]]:
    """entry key -> sorted dates it was put in front of someone.

    Both retrieval paths land here: auto-injection writes on its hook event, and
    recall.py writes `recall-query` / `recall-id` rows for need-driven pulls. If
    only one were logged, an entry somebody pulled and ignored would be counted
    as a delivery failure.
    """
    fires: dict[str, list[str]] = {}
    if not SURFACED_LOG.exists():
        return fires
    for line in SURFACED_LOG.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if TEST_SESSION.match(str(r.get("session", ""))):
            continue
        # The control arm: chosen to inject, deliberately not shown.
        if r.get("held_back"):
            continue
        entry, ts = str(r.get("entry", "")), str(r.get("ts", ""))[:10]
        if entry and ts:
            fires.setdefault(entry, []).append(ts)
    for k in fires:
        fires[k].sort()
    return fires


def load_recurrences() -> list[dict]:
    import yaml
    out = []
    for label, fname in CATALOGS.items():
        p = CATALOG_DIR / fname
        if not p.exists():
            continue
        try:
            data = yaml.safe_load(p.read_text())
        except Exception as exc:
            sys.stderr.write(f"measure: {label} catalog did not parse, EXCLUDED: {exc}\n")
            continue
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        for e in data or []:
            if not isinstance(e, dict) or not e.get("id"):
                continue
            n = rec_count(e)
            if not n:
                continue
            dates = []
            for k in e:
                m = DATED_KEY.match(str(k))
                if m:
                    dates.append(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
            out.append({"key": f"{label}:{e['id']}", "id": e["id"], "catalog": label,
                        "events": n, "dated": sorted(dates)})
    return out


def indexed_keys() -> set[str]:
    try:
        return set(json.loads(PROBE_INDEX.read_text())["entries"].keys())
    except Exception:
        return set()


def main() -> int:
    ap = argparse.ArgumentParser(prog="measure_prevention")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--gaps", action="store_true", help="only entries that recurred unsurfaced")
    a = ap.parse_args()

    fires = load_fires()
    recs = load_recurrences()
    idx = indexed_keys()
    window = sorted({d for ds in fires.values() for d in ds})
    lo, hi = (window[0], window[-1]) if window else ("", "")

    verdicts: Counter = Counter()
    rows = []
    for r in recs:
        for d in r["dated"]:
            if not window or d < lo or d > hi:
                v = "outside-log-window"
            else:
                prior = [f for f in fires.get(r["key"], []) if f <= d]
                v = "surfaced-then-recurred" if prior else "never-surfaced"
            verdicts[v] += 1
            rows.append({"key": r["key"], "date": d, "verdict": v,
                         "reachable": r["key"] in idx,
                         "fires_before": len([f for f in fires.get(r["key"], []) if f <= d])})
        undated = r["events"] - len(r["dated"])
        if undated > 0:
            verdicts["undated-unmeasurable"] += undated

    total = sum(verdicts.values())
    measurable = verdicts["surfaced-then-recurred"] + verdicts["never-surfaced"]
    out = {
        "recurrence_events": total,
        "measurable": measurable,
        "measurable_pct": round(100 * measurable / total, 1) if total else 0.0,
        "verdicts": dict(verdicts),
        "log_window": {"from": lo, "to": hi, "days": len(set(window))},
        "entries_with_recurrences": len(recs),
        "note": ("A recurrence recorded as a bare counter carries no date and cannot be placed "
                 "relative to a fire. Until recurrence bumps record a date, prevention is "
                 "unmeasurable for those events — that is a recording gap, not a null result."),
    }

    if a.json:
        print(json.dumps({"summary": out, "events": rows}, indent=2))
        return 0
    if a.gaps:
        gaps = [r for r in rows if r["verdict"] == "never-surfaced"]
        if not gaps:
            print("  no measurable never-surfaced recurrences")
            return 0
        print(f"  {len(gaps)} recurrence(s) where the lesson existed and never surfaced first:")
        for g in gaps:
            print(f"    {g['date']}  reachable={g['reachable']}  {g['key']}")
        return 0

    if not total:
        print("\n  No recurrences recorded yet — nothing has repeated, or nothing has been")
        print("  captured twice. This is the expected state for a new memory.\n")
        return 0

    print(f"\n  RECURRENCE EVENTS: {total}")
    print(f"  measurable:        {measurable} ({out['measurable_pct']}%)")
    print(f"  log window:        {lo} .. {hi} ({out['log_window']['days']} days)\n")
    for k in ("surfaced-then-recurred", "never-surfaced", "undated-unmeasurable", "outside-log-window"):
        if verdicts.get(k):
            print(f"    {verdicts[k]:5}  {k}")
    if measurable:
        s = verdicts["surfaced-then-recurred"]
        print(f"\n  Of what CAN be measured: {s}/{measurable} were surfaced first and recurred anyway.")
        print("    surfaced-then-recurred -> presentation/enforcement problem, not coverage")
        print("    never-surfaced         -> coverage problem, retrieval can help")
    print(f"\n  {out['note']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
