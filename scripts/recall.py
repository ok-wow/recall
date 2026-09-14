#!/usr/bin/env python3
"""recall — ask the okWOW • Recall corpus a question.

The loop had a capture half and a push half and no pull half. Injection only
ever reached entries whose `probe_when` contained BACKTICKED, code-shaped
literals, because build_probe_index.py extracts nothing else; an entry written
in prose yields zero tokens and is dropped from the index entirely. Measured on
2026-09-13: 592 of 1375 entries (43%) were unreachable, 426 of them despite
HAVING probe_when. Conceptual lessons -- most process failures -- could be
written down perfectly and never surface again.

This is the other half. It indexes the FULL TEXT of every entry with BM25, so
recall covers the whole corpus, and it is pull-only: the operator asks, so
precision can be looser than the injector's without teaching anyone to ignore a
channel. The precise literal matcher stays exactly as it is for auto-injection.

    recall.py "worktree node_modules symlink"   # ranked search
    recall.py --id <entry-id>                   # one entry in full
    recall.py --recurring                       # lessons that repeated anyway
    recall.py --unreachable                     # entries auto-injection cannot see
    recall.py --stats                           # corpus + coverage health
    recall.py --json "query"                    # machine-readable

No third-party dependencies: this must run inside a hook, a cron job, and a
fresh clone with nothing installed.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path

RECALL_HOME = Path(os.environ.get("RECALL_HOME") or Path.home() / ".recall")
CATALOG_DIR = Path(os.environ.get("RECALL_CATALOG_DIR") or RECALL_HOME / "catalogs")
# DE holds decisions and stated preferences rather than failures: what was
# chosen, what was rejected, and why. A failure log tells you what broke; it
# never tells you how the person you work with makes up their mind.
CATALOGS = {"FM": "FAILURE_MODES.yaml", "PF": "PROCESS_FAILURES.yaml",
            "DE": "DECISIONS.yaml"}
PROBE_INDEX = Path(os.environ.get("RECALL_PROBE_INDEX")
                   or RECALL_HOME / "probe-index.json").expanduser()

WORD = re.compile(r"[a-z0-9][a-z0-9._/-]*", re.I)
# Ordinary English that carries no retrieval signal. Deliberately short: BM25's
# idf already discounts common terms, and an over-eager stoplist is how a
# corpus stops answering questions phrased in plain language.
STOP = frozenset("""a an the and or but if then than that this these those is are was were be been being
do does did done have has had of in on at to for from by with without into over under about as it its
you your we our they their he she them us me my i not no yes can will would should could may might must
when where which who whom whose what why how all any both each few more most other some such only own
same so too very s t just now also there here up down out off again once""".split())

# Index EVERY string-ish value except pure metadata. A fixed allow-list of field
# names is wrong here: the corpus has 40+ distinct field names and `id` is the
# only one present in 100% of entries. An allow-list silently drops whatever an
# entry happened to call its body -- `test-runner-inherits-ambient-timezone...`
# stores its body in `verified_failure`, so an allow-list containing `what` and
# `failure` read that entry as almost empty and ranked it below noise.
SKIP_FIELDS = frozenset({
    "recurrences", "date", "logged", "compounded_at", "date_observed", "date_learned",
    "verification_date", "caught_in", "source_session", "synchronization", "probe_type",
    "verified_by_test", "verified_by_check", "fix_verified", "severity", "status",
    "confidence", "version", "updated_at", "created",
})
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def tokenize(text: str) -> list[str]:
    out = []
    for m in WORD.finditer(text.lower()):
        w = m.group(0).strip("._/-")
        if len(w) < 3 or w in STOP:
            continue
        out.append(w)
        # a/b/c and a.b.c also index their parts, so "node_modules symlink"
        # finds an entry that only ever wrote `worktree/node_modules`
        if len(out) < 4000:
            for part in re.split(r"[._/-]", w):
                if len(part) >= 3 and part not in STOP and part != w:
                    out.append(part)
    return out


def load_entries() -> list[dict]:
    try:
        import yaml
    except ImportError:
        sys.stderr.write("recall: PyYAML is required to read the catalogs\n")
        raise SystemExit(2)
    entries = []
    broken: list[dict] = []
    for label, fname in CATALOGS.items():
        path = CATALOG_DIR / fname
        if not path.exists():
            continue
        # Degrade, never die. A broken catalog used to raise a raw YAML traceback
        # out of here -- and because the health surface calls this to decide
        # whether to report, an unparseable catalog produced TOTAL SILENCE: the
        # index builder correctly refused, wrote a marker nothing reads, and the
        # one tool that could have said so crashed. Report the breakage as data
        # and keep serving whatever still parses.
        try:
            data = yaml.safe_load(path.read_text())
        except Exception as exc:
            first = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
            broken.append({"catalog": label, "path": str(path), "error": first})
            continue
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    data = v
                    break
        for e in data or []:
            if not isinstance(e, dict) or not e.get("id"):
                continue
            parts = []

            def harvest(val, depth=0):
                if depth > 4:
                    return
                if isinstance(val, str):
                    if not ISO_DATE.match(val.strip()):
                        parts.append(val)
                elif isinstance(val, (int, float)):
                    return
                elif isinstance(val, list):
                    for x in val:
                        harvest(x, depth + 1)
                elif isinstance(val, dict):
                    for k2, v2 in val.items():
                        if k2 not in SKIP_FIELDS:
                            harvest(v2, depth + 1)

            for f, v in e.items():
                if f in SKIP_FIELDS:
                    continue
                harvest(v)
            pw = e.get("probe_when")
            rec = e.get("recurrences")
            rec = len(rec) if isinstance(rec, list) else (rec if isinstance(rec, int) else 0)
            entries.append({
                "key": f"{label}:{e['id']}", "id": e["id"], "catalog": label,
                "recurrences": rec, "raw": e, "text": "\n".join(parts),
                "probe_when": pw if isinstance(pw, list) else ([pw] if pw else []),
            })
    load_entries.broken = broken
    return entries


def bm25(entries: list[dict], query: str, k1: float = 1.5, b: float = 0.75) -> list[tuple[dict, float, list[str]]]:
    q = [t for t in tokenize(query)]
    if not q:
        return []
    docs = [Counter(tokenize(e["text"])) for e in entries]
    lens = [sum(d.values()) or 1 for d in docs]
    avg = sum(lens) / len(lens) if lens else 1.0
    N = len(entries)
    df = Counter()
    for d in docs:
        for t in set(d) & set(q):
            df[t] += 1
    scored = []
    for e, d, dl in zip(entries, docs, lens):
        score, hits = 0.0, []
        for t in set(q):
            f = d.get(t, 0)
            if not f:
                continue
            idf = math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avg))
            hits.append(t)
        if score > 0:
            # a lesson that already repeated is worth surfacing above one that
            # never has -- recurrence is the corpus's own evidence of salience
            score *= 1.0 + 0.15 * min(e["recurrences"], 4)
            scored.append((e, score, sorted(hits)))
    scored.sort(key=lambda x: -x[1])
    return scored


def indexed_keys() -> set[str]:
    try:
        return set(json.loads(PROBE_INDEX.read_text())["entries"].keys())
    except Exception:
        return set()


def show(e: dict, score: float | None = None, hits: list[str] | None = None, full: bool = False) -> None:
    r = e["raw"]
    tag = f"  [{e['catalog']}]"
    rec = f"  ×{e['recurrences'] + 1}" if e["recurrences"] else ""
    head = f"{e['id']}{rec}"
    print(f"\n{head}\n{tag}" + (f"  score {score:.1f}  matched: {', '.join(hits[:6])}" if score is not None else ""))
    # "decided"/"why" are the DECISIONS shape; without them a decision entry
    # retrieves correctly and then prints an empty body, which reads as a bug.
    body = (r.get("what") or r.get("failure") or r.get("summary") or r.get("symptom")
            or r.get("trigger") or r.get("decided") or "")
    body = " ".join(str(body).split())
    print("  " + (body if full else body[:400] + ("…" if len(body) > 400 else "")))
    fix = r.get("fix_pattern") or r.get("fix") or r.get("why") or ""
    if fix:
        fix = " ".join(str(fix).split())
        # A decision has a reason, not a fix. Printing "FIX:" over a rationale
        # tells the reader the wrong thing about what they are looking at.
        label = "WHY" if e["catalog"] == "DE" else "FIX"
        print(f"  {label}: " + (fix if full else fix[:300] + ("…" if len(fix) > 300 else "")))
    if full and e["probe_when"]:
        print("  PROBE WHEN:")
        for p in e["probe_when"]:
            print(f"    - {p}")


def main() -> int:
    ap = argparse.ArgumentParser(prog="recall", description="Query the okWOW • Recall corpus.")
    ap.add_argument("query", nargs="*", help="free-text question")
    ap.add_argument("--id", help="show one entry by id")
    ap.add_argument("-n", "--limit", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--full", action="store_true", help="do not truncate bodies")
    ap.add_argument("--recurring", action="store_true", help="lessons that repeated anyway")
    ap.add_argument("--unreachable", action="store_true", help="entries auto-injection cannot see")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()

    entries = load_entries()
    for b in getattr(load_entries, "broken", []):
        sys.stderr.write(
            f"recall: {b['catalog']} catalog did not parse and is EXCLUDED from these "
            f"results — {b['path']}: {b['error']}\n")
    if not entries:
        sys.stderr.write(f"recall: no readable catalogs under {CATALOG_DIR}\n")
        return 2

    if a.stats:
        idx = indexed_keys()
        unreachable = [e for e in entries if e["key"] not in idx]
        noprobe = [e for e in unreachable if not e["probe_when"]]
        out = {
            "entries": len(entries),
            "by_catalog": dict(Counter(e["catalog"] for e in entries)),
            "recurring": sum(1 for e in entries if e["recurrences"]),
            "recurrence_events": sum(e["recurrences"] for e in entries),
            "reachable_by_injection": len(entries) - len(unreachable),
            "unreachable_by_injection": len(unreachable),
            "unreachable_pct": round(100 * len(unreachable) / len(entries), 1),
            "unreachable_missing_probe_when": len(noprobe),
            "unreachable_despite_probe_when": len(unreachable) - len(noprobe),
            "reachable_by_recall": len(entries),
            "unreadable_catalogs": getattr(load_entries, "broken", []),
        }
        print(json.dumps(out, indent=2) if a.json else
              "\n".join(f"  {k:34} {v}" for k, v in out.items()))
        return 0

    if a.id:
        m = [e for e in entries if e["id"] == a.id or e["key"] == a.id]
        if not m:
            near = [e["id"] for e in entries if a.id.lower() in e["id"].lower()][:5]
            sys.stderr.write(f"recall: no entry '{a.id}'" + (f"\n  did you mean: {', '.join(near)}\n" if near else "\n"))
            return 1
        if a.json:
            print(json.dumps(m[0]["raw"], indent=2, default=str))
        else:
            show(m[0], full=True)
        return 0

    if a.recurring:
        sel = sorted([e for e in entries if e["recurrences"]], key=lambda e: -e["recurrences"])[:a.limit]
    elif a.unreachable:
        idx = indexed_keys()
        sel = [e for e in entries if e["key"] not in idx][:a.limit]
    elif a.query:
        ranked = bm25(entries, " ".join(a.query))[:a.limit]
        if a.json:
            print(json.dumps([{"id": e["id"], "catalog": e["catalog"], "score": round(s, 2),
                               "matched": h, "recurrences": e["recurrences"]} for e, s, h in ranked], indent=2))
            return 0
        if not ranked:
            print("  no match")
            return 0
        for e, s, h in ranked:
            show(e, s, h, full=a.full)
        return 0
    else:
        ap.print_help()
        return 0

    if a.json:
        print(json.dumps([{"id": e["id"], "catalog": e["catalog"], "recurrences": e["recurrences"]} for e in sel], indent=2))
    else:
        for e in sel:
            show(e, full=a.full)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
