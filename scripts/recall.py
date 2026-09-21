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
    recall.py --hidden                          # statements no channel ever displays
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
from datetime import datetime, timezone
from pathlib import Path

# .expanduser() on both: build_probe_index.py and recall-probe-inject.py already
# do it, so RECALL_CATALOG_DIR=~/x used to index fine and then read as missing.
RECALL_HOME = Path(os.environ.get("RECALL_HOME") or Path.home() / ".recall").expanduser()
CATALOG_DIR = Path(os.environ.get("RECALL_CATALOG_DIR")
                   or RECALL_HOME / "catalogs").expanduser()
# DE holds decisions and stated preferences rather than failures: what was
# chosen, what was rejected, and why. A failure log tells you what broke; it
# never tells you how the person you work with makes up their mind.
CATALOGS = {"FM": "FAILURE_MODES.yaml", "PF": "PROCESS_FAILURES.yaml",
            "DE": "DECISIONS.yaml"}
PROBE_INDEX = Path(os.environ.get("RECALL_PROBE_INDEX")
                   or RECALL_HOME / "probe-index.json").expanduser()
# The SAME log auto-injection writes, so one analysis joins both retrieval paths.
# Logging only push made "never surfaced" mean "never AUTO-INJECTED": an entry a
# person or agent pulled and then ignored was counted as a delivery failure when
# it was a heeding failure. Those two need opposite fixes, so conflating them
# produces a number that cannot drive a decision.
SURFACED_LOG = Path(os.environ.get("RECALL_SURFACED_LOG")
                    or RECALL_HOME / "surfaced.jsonl").expanduser()

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
    parsed = 0
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
        parsed += 1
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
    load_entries.parsed = parsed
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



# Known names win, in priority order, because the RIGHT field should lead. But
# the corpus holds 80+ content-bearing field names with a long tail used once
# each -- evidence, cause, observed, how_to_apply, wrong_behavior, mechanism --
# and every hand-written list drifts behind the data within weeks. So when no
# known name matches, fall back to the longest unknown string on the entry.
# Self-healing: a field invented tomorrow displays tomorrow.
_META = {
    "id", "key", "catalog", "repo", "scope", "artifact", "date", "logged",
    "compounded_at", "source_session", "session", "probe_when", "probe_type",
    "probe_class", "failure_class", "recurrences", "caught_in", "related",
    "pr", "type", "title", "applies_to", "supersedes", "doctrine_link",
    "related_entry", "related_meta_learning", "verified_by_test",
    "verified_by_check", "discovered", "domain", "tell", "gap_found",
}


def _longest_unknown(raw: dict, used: str = "") -> str:
    best = ""
    for k, v in raw.items():
        if k in _META or k == used or not isinstance(v, str):
            continue
        if len(v.strip()) > len(best):
            best = v.strip()
    return best if len(best) >= 40 else ""


# The field names `show` prints from, hoisted out of it because a second
# consumer now needs to know what a reader actually sees. `--hidden` compares
# against these: a pasted copy drifts, and a drifted copy reports statements
# that are on screen while missing statements that are not.
#
# "decided"/"why" are the DECISIONS shape; without them a decision entry
# retrieves correctly and then prints an empty body, which reads as a bug.
# "pattern"/"consequence" are an older entry schema still in the corpus.
# 28 entries carried real content under them and rendered BLANK -- searchable,
# because BM25 indexes every string, and unreadable, because display did not
# know the names. A reader that does not know a field treats it as absent.
DISPLAY_BODY = ("what", "failure", "summary", "symptom", "trigger", "decided",
                "pattern", "consequence", "observed", "cause", "what_happened")
DISPLAY_FIX = ("fix_pattern", "fix", "why", "remedy", "rule", "lesson",
               "doctrine", "workaround", "how_to_apply", "correct_behavior")


def _first(raw: dict, fields: tuple[str, ...]):
    """First field with anything in it. Truthiness, not isinstance(str): a
    catalog occasionally stores a body as a list, and display already str()s
    whatever it gets."""
    for f in fields:
        v = raw.get(f)
        if v:
            return v
    return ""


def _push_channel():
    """The push half, imported rather than described.

    build_probe_index renders an injected entry through its own summarize() and
    remedy(), over field lists that are NOT the same as this file's -- it reads
    `what_failed` and `context`, which show() does not, and misses `what`, which
    show() leads with. A statement is only hidden when NEITHER channel would
    print it, so the audit has to ask the real function.

    Imported here and not at module scope because build_probe_index imports
    PyYAML at import time, and this file defers that so --help works without it.
    """
    if _push_channel.mod is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import build_probe_index
        _push_channel.mod = build_probe_index
    return _push_channel.mod


_push_channel.mod = None


def show(e: dict, score: float | None = None, hits: list[str] | None = None, full: bool = False) -> None:
    r = e["raw"]
    tag = f"  [{e['catalog']}]"
    rec = f"  ×{e['recurrences'] + 1}" if e["recurrences"] else ""
    head = f"{e['id']}{rec}"
    print(f"\n{head}\n{tag}" + (f"  score {score:.1f}  matched: {', '.join(hits[:6])}" if score is not None else ""))
    body = _first(r, DISPLAY_BODY) or _longest_unknown(r) or ""
    body = " ".join(str(body).split())
    print("  " + (body if full else body[:400] + ("…" if len(body) > 400 else "")))
    fix = _first(r, DISPLAY_FIX)
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


# A quoted statement short enough to be a label ("same bug", "see #12") carries
# no requirement, so 20 characters is the floor. Quotes are the signal: a note
# that QUOTES someone is a note recording words that were said to the team.
QUOTED = re.compile(r'"([^"]{20,})"')
# Half the words missing, deliberately crude. It catches a requirement that was
# never promoted into a displayed field. It does not judge wording, and it is
# not meant to: a subtler test would need to know what the words MEAN, and a
# check nobody can predict the output of gets switched off.
HIDDEN_MISSING_RATIO = 0.5


def _flatten(val) -> str:
    """A recurrence note is usually a string and sometimes a list or a map --
    `recurrence_log:` in one live corpus is a list of dated entries. str() on a
    list would work by accident (repr keeps the quotes); this works on purpose."""
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        return "\n".join(_flatten(v) for v in val)
    if isinstance(val, dict):
        return "\n".join(_flatten(v) for v in val.values())
    return str(val)


def displayed_text(e: dict) -> str:
    """Everything either channel actually puts in front of a reader.

    Assembled by CALLING the display code -- this file's own field lists and the
    push channel's summarize()/remedy() -- rather than by restating their field
    names a second time. probe_when counts: `--id --full` prints it. The
    `_longest_unknown` fallback counts too, and it is the reason this is a
    function and not a set of field names -- on an entry with no recognised body
    field, the longest unknown string IS the body on screen, and that can be a
    recurrence note, which is then not hidden at all.
    """
    raw, push = e["raw"], _push_channel()
    parts = [_first(raw, DISPLAY_BODY) or _longest_unknown(raw) or "",
             _first(raw, DISPLAY_FIX),
             # summarize/remedy truncate at 400 chars, and that truncation is
             # itself part of the answer: what push prints is what push prints.
             push.summarize(raw), push.remedy(raw)]
    parts += e["probe_when"]
    return "\n".join(str(p) for p in parts)


def hidden_statements(entries: list[dict]) -> list[dict]:
    """Quoted statements filed in a recurrence note that no channel will show.

    A recurrence note records the SAME requirement failing again. When it
    instead quotes someone ADDING or widening a requirement, that requirement is
    now written down and undeliverable. No delivery path renders a `recurrence_*`
    key: push prints summarize()/remedy(), pull prints show()'s fields, and
    neither list contains it. load_entries harvests every field, so the words
    still move BM25 -- the statement ranks and never appears. That gap is how a
    correction recorded faithfully on the day it was given was absent from the
    line an agent reads, and the same mistake repeated six days later.

    Compares against displayed_text(), which is derived from the display code
    rather than a copy of its field names, so a field added to either channel
    tomorrow stops producing findings tomorrow.
    """
    found = []
    for e in entries:
        notes = [(k, v) for k, v in e["raw"].items() if str(k).startswith("recurrence_")]
        if not notes:
            continue
        # One tokenizer for the whole tool. BM25 already decides what counts as
        # a word here, and a second vocabulary would let a statement be "hidden"
        # from a reader and "present" to the search that found it.
        shown = set(tokenize(displayed_text(e)))
        for key, note in notes:
            for quote in QUOTED.findall(_flatten(note)):
                said = set(tokenize(quote))
                missing = sorted(said - shown)
                if said and len(missing) / len(said) >= HIDDEN_MISSING_RATIO:
                    found.append({"id": e["id"], "catalog": e["catalog"], "key": key,
                                  "statement": " ".join(quote.split()),
                                  "missing": missing})
    return found


def log_pull(shown: list, mode: str, query: str = "") -> None:
    """Record entries a PULL actually put in front of someone.

    Only need-driven retrieval is logged -- a free-text query and `--id`. The
    browse modes are reports ABOUT the corpus, not a lesson delivered at a moment
    it was needed, and counting them would inflate surfacing with the act of
    auditing surfacing.

    The choice can only UNDER-count delivery, never flatter it. That bias is
    deliberate: it errs toward reporting a coverage failure, which is the safe
    direction for a metric whose job is to stop the loop marking its own homework.

    Never raises. A retrieval tool that fails because its telemetry failed is
    worse than no telemetry.
    """
    try:
        session = os.environ.get("RECALL_SESSION_ID") or "cli"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        SURFACED_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(SURFACED_LOG, "a", encoding="utf-8") as fh:
            for e, score, hits in shown:
                fh.write(json.dumps({
                    "ts": ts,
                    "session": session,
                    "event": mode,          # recall-query | recall-id
                    "entry": e["key"],
                    "score": round(score, 1) if isinstance(score, (int, float)) else None,
                    "tokens": hits,
                    "query": query[:200],
                }, separators=(",", ":")) + "\n")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(prog="recall", description="Query the okWOW • Recall corpus.")
    ap.add_argument("query", nargs="*", help="free-text question")
    ap.add_argument("--id", help="show one entry by id")
    ap.add_argument("-n", "--limit", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--full", action="store_true", help="do not truncate bodies")
    ap.add_argument("--recurring", action="store_true", help="lessons that repeated anyway")
    ap.add_argument("--unreachable", action="store_true", help="entries auto-injection cannot see")
    ap.add_argument("--hidden", action="store_true",
                    help="statements inside recurrence notes that no channel displays")
    ap.add_argument("--stubs", action="store_true",
                    help="entries with an id and no lesson — they count as covered and help nobody")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()

    entries = load_entries()
    for b in getattr(load_entries, "broken", []):
        sys.stderr.write(
            f"recall: {b['catalog']} catalog did not parse and is EXCLUDED from these "
            f"results — {b['path']}: {b['error']}\n")
    # A freshly installed corpus is EMPTY, and install.sh tells the user to run
    # `recall.py --stats` first. Treating zero entries as "no readable catalogs"
    # made a correct empty state indistinguishable from a broken install, and
    # made the first command a new user runs exit non-zero with a wrong reason.
    if not entries and not getattr(load_entries, "parsed", 0):
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
            # 0.0 on an empty corpus rather than ZeroDivisionError: a fresh
            # install has no entries and --stats is the first thing it is told
            # to run. The old "no readable catalogs" guard returned early and
            # hid this; removing that guard is what surfaced it.
            "unreachable_pct": round(100 * len(unreachable) / len(entries), 1) if entries else 0.0,
            "unreachable_missing_probe_when": len(noprobe),
            "unreachable_despite_probe_when": len(unreachable) - len(noprobe),
            "reachable_by_recall": len(entries),
            # Counted here because a corpus can be 100% reachable and still be
            # failing to deliver: reachability is per ENTRY, this is per
            # STATEMENT inside an entry that is already reachable.
            "hidden_statements": len(hidden_statements(entries)),
            "unreadable_catalogs": getattr(load_entries, "broken", []),
        }
        print(json.dumps(out, indent=2) if a.json else
              "\n".join(f"  {k:34} {v}" for k, v in out.items()))
        return 0

    if a.hidden:
        # Every finding, never a page of them. -n ranks the browse modes; this
        # is an audit, and an audit that silently stops at five under-reports
        # the one number it exists to produce.
        found = hidden_statements(entries)
        if a.json:
            print(json.dumps(found, indent=2))
            return 0
        if not found:
            print(f"  {len(entries)} entries, no statement hidden inside a recurrence note")
            return 0
        print(f"{len(found)} statement(s) inside a recurrence note that no channel displays:\n")
        for f in found:
            print(f"  {f['id']}  ·  {f['key']}")
            print(f"    said: \"{f['statement'][:140]}\"")
            print(f"    never displayed: {', '.join(f['missing'][:8])}\n")
        print("Put the requirement in a displayed field, or give it its own entry.")
        # 0 even when it finds something, like every other report mode here.
        # Whether a backlog of unpromoted statements blocks anything is the
        # corpus owner's policy, and a corpus is never clean the way one file
        # can be -- a gate that is red on day one is a gate someone removes.
        return 0

    if a.id:
        m = [e for e in entries if e["id"] == a.id or e["key"] == a.id]
        if not m:
            # str() on both sides: a catalog may carry `id: 12345` or a bare YAML
            # date, and load_entries admits any truthy id. .lower() on an int
            # crashed this "did you mean" scan with a raw traceback.
            near = [str(e["id"]) for e in entries
                    if a.id.lower() in str(e["id"]).lower()][:5]
            sys.stderr.write(f"recall: no entry '{a.id}'" + (f"\n  did you mean: {', '.join(near)}\n" if near else "\n"))
            return 1
        log_pull([(m[0], None, [])], "recall-id", str(a.id))
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
    elif a.stubs:
        # An id with no body AND no fix is indistinguishable from a covered
        # lesson in every count, and cannot be acted on by anyone. The id still
        # carries real signal, so this lists them to be FILLED, not deleted:
        # removing 28 named lessons to improve a coverage number is the exact
        # move this corpus exists to catch.
        BODY = ("trigger", "summary", "what_failed", "context", "symptom",
                "failure", "what", "decided")
        FIX = ("fix_pattern", "fix", "affected_pattern", "why")
        def _empty(e, keys):
            return not any(str(e["raw"].get(k) or "").strip() for k in keys)
        sel = [e for e in entries if _empty(e, BODY) and _empty(e, FIX)][:a.limit]
    elif a.query:
        q = " ".join(a.query)
        ranked = bm25(entries, q)[:a.limit]
        log_pull(ranked, "recall-query", q)
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
