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
    recall.py --specs "clarification form"      # the spec corpus only
    recall.py --hub "prototype"                 # the published hub, with URLs
    recall.py --connectors "pricing"            # Slack / Gmail / meeting pointers
    recall.py --lot                             # parked work, most urgent first
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
def env_path(*names_then_default) -> Path:
    """First of several env vars that is set, else the default.

    Two names because this file has two lineages that were merged: the shipped
    tool names its variables RECALL_*, and the okwow-compound install that has
    been running it names them OKWOW_*. Both sets of tests and hooks are still
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
# Every skill that carries a rules.yaml is part of the searchable corpus. See
# load_rules for why they are indexed but scored separately.
SKILLS_DIR = env_path("RECALL_SKILLS_DIR", "OKWOW_SKILLS_DIR",
                      Path.home() / ".claude/skills")
# DE holds decisions and stated preferences rather than failures: what was
# chosen, what was rejected, and why. A failure log tells you what broke; it
# never tells you how the person you work with makes up their mind.
CATALOGS = {"FM": "FAILURE_MODES.yaml", "PF": "PROCESS_FAILURES.yaml",
            "DE": "DECISIONS.yaml"}
PROBE_INDEX = env_path("RECALL_PROBE_INDEX", "OKWOW_PROBE_INDEX",
                       RECALL_HOME / "probe-index.json")
# The SAME log auto-injection writes, so one analysis joins both retrieval paths.
# Logging only push made "never surfaced" mean "never AUTO-INJECTED": an entry a
# person or agent pulled and then ignored was counted as a delivery failure when
# it was a heeding failure. Those two need opposite fixes, so conflating them
# produces a number that cannot drive a decision.
SURFACED_LOG = env_path("RECALL_SURFACED_LOG", "OKWOW_PROBE_SURFACED_LOG",
                        RECALL_HOME / "surfaced.jsonl")
# The specs corpus. Its index is already built and already gated -- see
# load_specs -- and until 2026-09-21 it was read by nothing.
SPECS_INDEX = env_path("RECALL_SPECS_INDEX", "OKWOW_SPECS_INDEX",
                       Path.home() / "dev/specs/llms.txt")
# The published hub -- specs, plans, playbooks, prototypes, strategy, pulse.
# Also an index that already existed and was read by nothing. See load_hub.
HUB_INDEX = env_path("RECALL_HUB_INDEX", "OKWOW_HUB_INDEX",
                     Path.home() / "dev/briefings/site/_index.json")
HUB_BASE = os.environ.get("RECALL_HUB_BASE") or "https://internal.okwow.ai"
# Pointers into Slack, Gmail, meetings -- written by connector_index.py, which
# refuses anything longer than a gist. See load_connectors.
RECEIPT_DIR = env_path("RECALL_RECEIPT_DIR", "OKWOW_RECEIPT_DIR",
                       Path.home() / ".okwow/local-learning-receipts")
ORPHAN_INDEX = env_path("RECALL_ORPHAN_INDEX", "OKWOW_ORPHAN_INDEX",
                        Path.home() / ".claude/skills/_orphans/learnings.yaml")
CONNECTOR_DIR = env_path("RECALL_CONNECTOR_DIR", "OKWOW_CONNECTOR_DIR",
                         RECALL_HOME / "connectors")
# Work somebody decided to do later, one JSON file per item, written by
# park.py. See read_lot.
LOT_DIR = env_path("RECALL_LOT_DIR", "OKWOW_LOT_DIR", RECALL_HOME / "lot" / "items")

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
    entries.extend(load_rules(broken))
    entries.extend(load_specs(broken))
    entries.extend(load_hub(broken))
    entries.extend(load_connectors(broken))
    entries.extend(load_receipts(broken))
    entries.extend(load_orphans(broken))
    entries.extend(load_lot(broken))
    load_entries.broken = broken
    load_entries.parsed = parsed
    return entries


def load_rules(broken: list[dict]) -> list[dict]:
    """Index the rules.yaml files of the kind:rules skills.

    Same degrade-never-die contract as the catalogs: an unparseable rules file
    is reported as data and the rest still serve. Rules carry no probe_when and
    are not in the injection index by design -- they reach a session when their
    skill fires -- so --stats scores injection over the catalogs alone and
    counts these separately. Folding them in would have moved a tracked metric
    by 200 entries without anything actually getting less reachable.
    """
    import yaml            # deferred for the same reason load_entries defers it
    out = []
    for rf in sorted(SKILLS_DIR.glob("*/rules.yaml")):
        skill = rf.parent.name
        # The display tag is the owning skill, minus the provenance prefix that
        # is true of nearly everything: [doctrine], not [okwow-doctrine].
        label = skill[6:] if skill.startswith("okwow-") else skill
        try:
            data = yaml.safe_load(rf.read_text()) or {}
        except Exception as exc:
            first = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
            broken.append({"catalog": label, "path": str(rf), "error": first})
            continue
        rules = data.get("rules") if isinstance(data, dict) else data
        for r in rules or []:
            if not isinstance(r, dict) or not r.get("id"):
                continue
            parts = []
            for f, v in r.items():
                if f in SKIP_FIELDS:
                    continue
                if isinstance(v, str) and not ISO_DATE.match(v.strip()):
                    parts.append(v)
                elif isinstance(v, list):
                    parts.extend(str(x) for x in v if isinstance(x, (str, int, float)))
            # The owning skill is part of what you are searching for: "which
            # skill holds the rule about X" is a real question this answers.
            parts.append(skill)
            out.append({
                "key": f"{label}:{r['id']}", "id": r["id"], "catalog": label,
                "recurrences": 0, "raw": r, "text": "\n".join(parts),
                "probe_when": [], "is_rule": True, "skill": skill,
            })
    return out


# One line per spec in llms.txt:
#   - [Title](./slug/spec.md): workspace · type · status · updated DATE · tldr ([preview](url))
SPEC_LINE = re.compile(r"^- \[(?P<title>.+?)\]\((?P<path>\.[^)]+)\):\s*(?P<meta>.+)$")
SPEC_PREVIEW = re.compile(r"\s*\(\[preview\]\([^)]*\)\)\s*$")
SPEC_TITLE_SUFFIX = re.compile(r"\s*·\s*spec$", re.I)
# The one element of the metadata chain with a fixed shape. See load_specs.
SPEC_UPDATED = re.compile(r"\s·\supdated\s(\d{4}-\d{2}-\d{2})\s·\s")


def load_specs(broken: list[dict]) -> list[dict]:
    """Index the spec corpus from ~/dev/specs/llms.txt.

    The catalogs answer "what went wrong before". Nothing answered "what does
    this product do" or "what did we already decide about X": on 2026-09-21
    all 197 spec folders sat outside every retrieval path, so a session had no
    route to them and re-derived what a spec had already settled. The count is
    read from the file on every run and only grows -- nothing here is sized to
    a snapshot of it.

    llms.txt and not the 197 spec.md bodies, for three reasons. It is already
    the distilled form -- slug, type, status, updated, tldr. It is regenerated
    by specs/_scripts/build-llms-txt.mjs, and a pre-commit check refuses a
    commit where it has drifted from the specs on disk, so it cannot go stale
    behind a committed spec. And the bodies are ~2 MB of design prose, which is
    not something to push through a BM25 pass that runs inside a hook.

    Specs carry no probe_when and are not in the injection index, so --stats
    counts them separately for the same reason it separates rules: folding them
    in would move a tracked percentage without anything becoming less reachable.
    """
    if not SPECS_INDEX.exists():
        return []
    try:
        text = SPECS_INDEX.read_text()
    except Exception as exc:
        first = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        broken.append({"catalog": "SPEC", "path": str(SPECS_INDEX), "error": first})
        return []
    out, section = [], ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        m = SPEC_LINE.match(line)
        if not m:
            continue
        slug = m.group("path").strip("./").split("/")[0]
        if not slug:
            continue
        meta = SPEC_PREVIEW.sub("", m.group("meta"))
        # Anchor on `· updated <date> ·`; do not count fields from the left.
        # The chain is workspace · type… · status, and how many type segments
        # it carries VARIES -- 13 of 159 lines carried an extra one on
        # 2026-09-21. A positional read shifted every one of those by a field,
        # so the body printed "updated 2026-05-28 · …" and the first clause of
        # the actual tldr was thrown away. The date is the only element with a
        # fixed shape, which makes it the only thing safe to anchor on, and the
        # corpus is a growing file written by a generator that will keep
        # gaining fields.
        u = SPEC_UPDATED.search(meta)
        if u:
            head, updated, tldr = meta[:u.start()], f"updated {u.group(1)}", meta[u.end():]
        else:
            # Keep the spec, lose only its metadata. Dropping the line would
            # make a format change look like a spec that does not exist.
            head, updated, tldr = "", "", meta
        chain = [b.strip() for b in head.split(" · ") if b.strip()]
        status = chain[-1] if len(chain) > 1 else ""
        doc_type = " · ".join(chain[1:-1]) if len(chain) > 2 else ""
        title = SPEC_TITLE_SUFFIX.sub("", m.group("title")).strip()
        out.append({
            "key": f"SPEC:{slug}", "id": slug, "catalog": "SPEC",
            "recurrences": 0, "probe_when": [], "is_spec": True,
            "raw": {"id": slug, "title": title, "summary": tldr,
                    "spec_status": status, "doc_type": doc_type,
                    "updated": updated, "section": section,
                    "spec_path": str(SPECS_INDEX.parent / slug / "spec.md")},
            # The slug is indexed twice, once literally and once with its
            # hyphens opened out, because "the clarification form spec" and
            # `clarification-form-keyboard-ux` should both find it by name.
            "text": "\n".join([slug, slug.replace("-", " "), title, tldr,
                               doc_type, status, section]),
        })
    # The file exists and its shape is gated, so zero parsed lines means the
    # generator changed its format -- not that there are no specs. Staying
    # silent there would read exactly like an empty spec corpus.
    if not out:
        broken.append({"catalog": "SPEC", "path": str(SPECS_INDEX),
                       "error": "no spec lines matched the expected format"})
    return out


def load_hub(broken: list[dict]) -> list[dict]:
    """Index the published hub from briefings/site/_index.json.

    The hub is where a spec, plan, playbook or prototype becomes something a
    person can OPEN -- which is exactly what a session needs to hand someone,
    and exactly what it could not find. Its generator already writes a
    machine-readable index (schema okwow-hub-index-v1, two buckets: `specs` and
    `artifacts`), and on 2026-09-21 nothing read it either: 91 entries, 0
    retrievable.

    Overlaps `load_specs` on purpose. They answer different questions -- the
    spec corpus answers "what did we decide", the hub answers "what can I send
    someone" -- and a published spec carries a URL its working copy does not.
    Deduping them would lose the URL, which is the reason to index the hub.
    """
    if not HUB_INDEX.exists():
        return []
    try:
        data = json.loads(HUB_INDEX.read_text())
    except Exception as exc:
        first = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        broken.append({"catalog": "HUB", "path": str(HUB_INDEX), "error": first})
        return []
    out = []
    # Read every list the file carries rather than the two bucket names known
    # today: the generator owns this schema and will add buckets. A hardcoded
    # ("specs", "artifacts") silently drops whatever it names next.
    buckets = [v for v in data.values() if isinstance(v, list)] if isinstance(data, dict) else [data]
    for bucket in buckets:
        for i in bucket:
            if not isinstance(i, dict):
                continue
            href = str(i.get("href") or "").strip()
            slug = str(i.get("slug") or "").strip() or href.strip("/").split("/")[-1]
            if not slug:
                continue
            # The first path segment is the hub section -- specs, plans,
            # playbooks, prototypes -- and it is what a reader is filtering by
            # when they ask for "the playbooks".
            section = href.strip("/").split("/")[0] if "/" in href.strip("/") else ""
            body = str(i.get("tldr") or i.get("description") or "").strip()
            title = str(i.get("title") or "").strip()
            out.append({
                "key": f"HUB:{section}/{slug}" if section else f"HUB:{slug}",
                "id": slug, "catalog": "HUB", "recurrences": 0,
                "probe_when": [], "is_hub": True,
                "raw": {"id": slug, "title": title, "summary": body or title,
                        "spec_status": str(i.get("status") or "").strip(),
                        "doc_type": section,
                        "updated": str(i.get("updatedAt") or "").strip()[:10],
                        "spec_path": f"{HUB_BASE}/{href.lstrip('/')}" if href else ""},
                "text": "\n".join([slug, slug.replace("-", " "), title, body,
                                   section, str(i.get("track") or "")]),
            })
    if not out:
        broken.append({"catalog": "HUB", "path": str(HUB_INDEX),
                       "error": "no entries matched the expected shape"})
    return out


# A loader's `except Exception` cannot tell "this file is malformed" from "this
# code is broken", and it reports both as the first. Flagged 2026-09-22 by a
# parallel session reading a NameError as a parse failure: the message blamed
# the receipts for a missing import. The data case is recoverable and expected
# -- one corrupt file must not cost the other 265. A programming error is
# neither, and saying so in the message is the difference between fixing a file
# and fixing the reader.
DATA_ERRORS = (ValueError, UnicodeDecodeError, OSError, KeyError, TypeError,
               AttributeError)


def why_broken(exc: Exception) -> str:
    """The error line, marked when the cause is this code rather than the file."""
    first = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    if isinstance(exc, (NameError, ImportError, IndentationError, SyntaxError)):
        return f"BUG IN RECALL, not in this file — {exc.__class__.__name__}: {first}"
    return first


def load_receipts(broken: list[dict]) -> list[dict]:
    """Index the local learning receipts -- the working notes, not the verdicts.

    A receipt is what was known at one moment, with its locators, its authority
    and its uncertainty intact. A catalog entry is the settled conclusion. The
    two are different jobs and merging them would lose the provenance half.

    What was wrong until 2026-09-22 is that only the second half was findable.
    266 receipts sat outside every search path, so the working notes accumulated
    where nothing could reach them -- the same disease as 87 parked orphans and
    29 undescribed doctrine rules, a third time. Indexing them does not promote
    anything; it just means a candidate can be FOUND before it is re-derived.

    Two shapes on disk: the contract's `<id>/v<N>.json` versioned directory, and
    flat `.md`/`.json`/`.yaml` files from before it. Both are read; the newest
    version of a directory wins.
    """
    import yaml            # deferred, as every loader here does
    if not RECEIPT_DIR.exists():
        return []
    out = []

    def add(rid: str, title: str, body: str, when: str, path: Path, ver: str = ""):
        body = " ".join(str(body).split())
        out.append({
            "key": f"RECEIPT:{rid}", "id": rid, "catalog": "RECEIPT",
            "recurrences": 0, "probe_when": [], "is_receipt": True,
            "raw": {"id": rid, "title": title or rid,
                    "summary": body[:400] or title or rid,
                    "doc_type": f"receipt{' ' + ver if ver else ''}",
                    "updated": when, "spec_path": str(path)},
            "text": "\n".join([rid.replace("-", " "), title, body]),
        })

    for item in sorted(RECEIPT_DIR.iterdir()):
        try:
            if item.is_dir():
                vs = sorted(item.glob("v*.json"),
                            key=lambda f: int(f.stem[1:]) if f.stem[1:].isdigit() else 0)
                if not vs:
                    continue
                d = json.loads(vs[-1].read_text())
                body = json.dumps(d.get("candidate") or d.get("claims") or d,
                                  ensure_ascii=False)
                add(item.name, str(d.get("revision_reason") or ""), body,
                    str(d.get("captured_at") or "")[:10], vs[-1], vs[-1].stem)
            elif item.suffix in (".json", ".yaml", ".yml", ".md"):
                raw = item.read_text()
                if item.suffix == ".md":
                    add(item.stem, "", raw, "", item)
                else:
                    d = (json.loads(raw) if item.suffix == ".json"
                         else yaml.safe_load(raw))
                    if not isinstance(d, dict):
                        continue
                    add(str(d.get("id") or d.get("receipt_id") or item.stem),
                        str(d.get("title") or d.get("revision_reason") or ""),
                        json.dumps(d, ensure_ascii=False, default=str),
                        str(d.get("captured_at") or "")[:10], item)
        except Exception as exc:
            broken.append({"catalog": "RECEIPT", "path": str(item),
                           "error": why_broken(exc)})
    return out


def load_orphans(broken: list[dict]) -> list[dict]:
    """Index the orphanage -- learning that was captured and had no home yet.

    A parked orphan is the worst state in the system: captured, so it reads as
    handled, and outside every search path, so it cannot be reached. 87 of them
    on 2026-09-22, with three categories over the promotion threshold and
    `okwow-promote` never once run. Indexing them does not promote them. It
    means the next session asking the question finds the note instead of
    starting the thought again.
    """
    import yaml            # deferred, as every loader here does
    if not ORPHAN_INDEX.exists():
        return []
    try:
        doc = yaml.safe_load(ORPHAN_INDEX.read_text()) or {}
    except Exception as exc:
        broken.append({"catalog": "ORPHAN", "path": str(ORPHAN_INDEX),
                       "error": why_broken(exc)})
        return []
    out = []
    for o in (doc.get("orphans") or []):
        if not isinstance(o, dict):
            continue
        # Only a PARKED orphan is unhomed. A promoted one lives at promoted_to,
        # often corrected on the way: of 17 code facts promoted on 2026-09-22,
        # 9 changed and 2 were false ("the backend has no MCP client"). Indexing
        # the parked text would keep serving the version that was fixed.
        if str(o.get("status") or "parked") != "parked":
            continue
        oid = str(o.get("id") or o.get("name") or "")
        if not oid:
            continue
        body = " ".join(str(v) for k, v in o.items()
                        if k not in ("id", "name") and isinstance(v, (str, int, float)))
        cat = str(o.get("proposed_category") or "")
        out.append({
            "key": f"ORPHAN:{oid}", "id": oid, "catalog": "ORPHAN",
            "recurrences": 0, "probe_when": [], "is_orphan": True,
            "raw": {"id": oid, "title": oid.replace("-", " "),
                    "summary": " ".join(body.split())[:400],
                    "doc_type": f"orphan · {o.get('status') or 'parked'}"
                                + (f" · {cat}" if cat else ""),
                    "updated": str(o.get("captured_at") or o.get("date") or "")[:10],
                    "spec_path": str(ORPHAN_INDEX)},
            "text": "\n".join([oid.replace("-", " "), body, cat,
                              str(o.get("proposed_artifact_slug") or "")]),
        })
    return out


# The parking lot's contract, shared with park.py, which writes it. Kept here
# because recall reads the lot and park.py already imports recall; one
# direction of import, one copy of the rules.
LOT_OPEN = ("parked", "in-progress")
LOT_TIER_LABEL = {1: "do next", 2: "soon", 3: "someday"}
# A "do next" list with twenty items in it is a list nobody reads. See park.py.
LOT_TIER1_CAP = 5


def read_lot(broken: list[dict]) -> list[dict]:
    """Every item in the parking lot, whatever its status.

    One file per item so parallel sessions never merge a shared file. The file
    name is the item's identity, because it is what park.py writes to. An
    unreadable file is reported and skipped; it must not cost the rest.
    """
    if not LOT_DIR.exists():
        return []
    items = []
    for f in sorted(LOT_DIR.glob("*.json")):
        try:
            item = json.loads(f.read_text())
            if not isinstance(item, dict) or not str(item.get("title") or "").strip():
                raise ValueError("not a parked item: it has no title")
        except Exception as exc:
            broken.append({"catalog": "LOT", "path": str(f), "error": why_broken(exc)})
            continue
        item["id"] = f.stem
        items.append(item)
    return items


def load_lot(broken: list[dict]) -> list[dict]:
    """Index the open items in the parking lot -- work deferred, not lessons.

    "Do this later" used to be a line in a handoff, and nothing ever showed a
    handoff line again. Indexed here, a question that touches the work finds
    it. Done and killed items stay on disk for their history and leave the
    index, so a finished plan never answers a question as if it were pending.
    """
    out = []
    for i in read_lot(broken):
        if str(i.get("status") or "parked") not in LOT_OPEN:
            continue
        src = i.get("source") if isinstance(i.get("source"), dict) else {}
        out.append({
            "key": f"LOT:{i['id']}", "id": i["id"], "catalog": "LOT",
            "recurrences": 0, "probe_when": [], "is_lot": True, "raw": i,
            "text": "\n".join(str(x) for x in (
                i["id"].replace("-", " "), i.get("title"), i.get("body"),
                i.get("first_move"), i.get("theme"), src.get("quote"),
                src.get("ref")) if x),
        })
    return out


def lot_tier(item: dict) -> int | None:
    t = item.get("tier")
    # bool first: True == 1, so a hand-edited "tier": true would read as tier 1.
    return t if not isinstance(t, bool) and t in (1, 2, 3) else None


def lot_tier1_open(items: list[dict]) -> int:
    return sum(1 for i in items if lot_tier(i) == 1
               and str(i.get("status") or "parked") in LOT_OPEN)


def lot_order(items: list[dict]) -> list[dict]:
    """Tier 1, 2, 3, then the unsorted inbox; the owner's own asks first
    within each; then the most recently touched."""
    items = sorted(items, key=lambda i: str(i.get("last_touched") or ""), reverse=True)
    return sorted(items, key=lambda i: (lot_tier(i) or 4, not i.get("owner_said")))


def lot_table(items: list[dict], tier1_open: int | None = None) -> str:
    """The list a person scans: tier, id, title, theme, age in days.

    tier1_open is counted by the caller over the whole store, because a
    filtered list cannot tell how full tier 1 is.
    """
    if not items:
        return "  nothing parked"
    today = datetime.now(timezone.utc).date()
    rows = []
    for i in items:
        try:
            age = f"{(today - datetime.strptime(str(i.get('created'))[:10], '%Y-%m-%d').date()).days}d"
        except ValueError:
            age = "?"
        title = " ".join(str(i.get("title") or "").split())
        rows.append((f"{lot_tier(i) or 'inbox'}{'*' if i.get('owner_said') else ''}", i["id"],
                     title if len(title) <= 50 else title[:49] + "…",
                     str(i.get("theme") or "other"), age))
    head = ("tier", "id", "title", "theme", "age")
    w = [max(len(r[c]) for r in rows + [head]) for c in range(4)]
    lines = ["  " + "  ".join(r[c].ljust(w[c]) for c in range(4)) + "  " + r[4]
             for r in [head] + rows]
    lines.append(f"\n  {len(items)} listed"
                 + (f" · tier 1 holds {tier1_open} of {LOT_TIER1_CAP}" if tier1_open is not None else "")
                 + (" · * the owner asked for it" if any(i.get("owner_said") for i in items) else ""))
    return "\n".join(lines)


def lot_lines(r: dict, full: bool = False) -> list[str]:
    """A parked item is a plan: what it is, how urgent, where it came from,
    and the first thing to do. It has no fix, so it does not print one."""
    out = [f"  {' '.join(str(r.get('title') or '').split())}"]
    body = " ".join(str(r.get("body") or "").split())
    if body:
        out.append("  " + (body if full else body[:BODY_CAP] + ("…" if len(body) > BODY_CAP else "")))
    t = lot_tier(r)
    out.append(f"  {f'tier {t} · {LOT_TIER_LABEL[t]}' if t else 'not sorted'} · "
               f"{r.get('status') or 'parked'} · {r.get('theme') or 'other'}"
               + (" · the owner asked for it" if r.get("owner_said") else ""))
    src = r.get("source") if isinstance(r.get("source"), dict) else {}
    kind, ref = src.get("kind"), src.get("ref")
    where = " · ".join(x for x in (
        f"session {src['session_id']}" if src.get("session_id") else "",
        f"{kind} {ref}" if ref else (kind if kind and kind != "session" else "")) if x)
    if where or src.get("quote"):
        out.append(f"  from: {where or '?'}" + (f' — "{src["quote"]}"' if src.get("quote") else ""))
    if r.get("first_move"):
        out.append(f"  FIRST MOVE: {r['first_move']}")
    if full:
        links = r.get("links") if isinstance(r.get("links"), dict) else {}
        for label, v in (("linear", links.get("linear")), ("prs", ", ".join(links.get("prs") or [])),
                         ("spec", links.get("spec")),
                         ("revisit when", "; ".join(r.get("revisit_when") or []))):
            if v:
                out.append(f"  {label}: {v}")
        out.append(f"  created {str(r.get('created'))[:10]} · touched {str(r.get('last_touched'))[:10]}"
                   f" · {len(r.get('history') or [])} change(s)")
        out.append(f"  {LOT_DIR / (str(r.get('id')) + '.json')}")
    return out


def load_connectors(broken: list[dict]) -> list[dict]:
    """Index the connector pointers under $RECALL_HOME/connectors/*.jsonl.

    Measured 2026-09-21, against a live Slack search: a connector tool call and
    a local query cost about the same per lookup (~700 tokens vs ~582). The
    index is not here to be cheaper. It is here for the two things a tool call
    structurally cannot do.

    First, a tool call requires knowing WHICH app to search, and the whole
    reason the corpus went unread is that nobody knew where to look -- the
    connector question had been answered in `core-world-model` four months
    earlier. Ranking every source in one list removes that decision. Second,
    Slack's search is lexical-only on this account (the tool says so), which is
    the same blind spot that leaves 43% of the catalog unreachable; indexed
    locally, a thread gets BM25 with token-splitting and the Jev rerank.

    Bodies are deliberately absent -- connector_index.py refuses them -- so a
    hit here is a pointer. Read the gist, then fetch the body through the
    connector if it turns out to matter.
    """
    if not CONNECTOR_DIR.exists():
        return []
    out = []
    for f in sorted(CONNECTOR_DIR.glob("*.jsonl")):
        source = f.stem
        try:
            lines = f.read_text().splitlines()
        except Exception as exc:
            first = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
            broken.append({"catalog": source.upper(), "path": str(f), "error": first})
            continue
        bad = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                bad += 1                       # one crashed write, not a dead source
                continue
            if not isinstance(r, dict) or not r.get("id"):
                bad += 1
                continue
            people = r.get("people") if isinstance(r.get("people"), list) else []
            title, gist = str(r.get("title") or ""), str(r.get("gist") or "")
            out.append({
                "key": f"{source.upper()}:{r['id']}", "id": r["id"],
                "catalog": source.upper(), "recurrences": 0, "probe_when": [],
                "is_connector": True,
                "raw": {"id": r["id"], "title": title, "summary": gist or title,
                        "spec_status": str(r.get("where") or ""),
                        # scope rides in doc_type so the existing spec/hub
                        # rendering prints it without a second code path.
                        "doc_type": f"{source} · {r['scope']}" if r.get("scope")
                                    and r["scope"] != "unscoped" else source,
                        "updated": str(r.get("date") or "")[:10],
                        "spec_path": str(r.get("url") or "")},
                # People are indexed: "what did Zachary say about X" is the
                # question a thread index answers and a spec index cannot.
                "text": "\n".join([title, gist, " ".join(str(p) for p in people),
                                   str(r.get("where") or ""), source]),
            })
        if bad:
            broken.append({"catalog": source.upper(), "path": str(f),
                           "error": f"{bad} unreadable line(s) skipped"})
    return out


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
# show() truncates here. judge_text() reuses the same two caps deliberately: a
# ranker should order what the reader will actually see, not a fuller version
# of it that nobody is shown.
BODY_CAP = 400
FIX_CAP = 300


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


# Sources that are pointers or plans rather than lessons. None of them reaches
# the push channel, so an injection-coverage number or a cut-off audit that
# counted them would move without anything getting harder to reach. One list,
# so the next source is excluded everywhere at once instead of at four sites.
NOT_LESSONS = ("is_spec", "is_hub", "is_connector", "is_receipt", "is_orphan", "is_lot")


def is_pointer(e: dict) -> bool:
    return any(e.get(f) for f in NOT_LESSONS)


def show(e: dict, score: float | None = None, hits: list[str] | None = None,
         full: bool = False, judged: float | None = None) -> None:
    r = e["raw"]
    tag = f"  [{e['catalog']}]"
    rec = f"  ×{e['recurrences'] + 1}" if e["recurrences"] else ""
    head = f"{e['id']}{rec}"
    line = f"  score {score:.1f}  matched: {', '.join(hits[:6])}" if score is not None else ""
    # Both numbers, because they answer different questions: the search score
    # says why this entry was fetched at all, the judged score says why it is
    # in this position.
    if judged is not None:
        line = f"  judged {judged:.2f}" + line
    print(f"\n{head}\n{tag}" + line)
    if e.get("is_lot"):
        print("\n".join(lot_lines(r, full)))
        return
    body = _first(r, DISPLAY_BODY) or _longest_unknown(r) or ""
    body = " ".join(str(body).split())
    print("  " + (body if full else body[:BODY_CAP] + ("…" if len(body) > BODY_CAP else "")))
    if is_pointer(e):
        # A spec has no fix. Its state and its path are what a reader needs
        # next, and the path is the whole point of retrieving it.
        meta = " · ".join(x for x in (r.get("doc_type"), r.get("spec_status"),
                                      r.get("updated")) if x)
        if meta:
            print(f"  {meta}")
        print(f"  {r.get('spec_path', '')}")
    fix = _first(r, DISPLAY_FIX)
    if fix:
        fix = " ".join(str(fix).split())
        # A decision has a reason, not a fix. Printing "FIX:" over a rationale
        # tells the reader the wrong thing about what they are looking at.
        label = "WHY" if e["catalog"] == "DE" else "FIX"
        print(f"  {label}: " + (fix if full else fix[:FIX_CAP] + ("…" if len(fix) > FIX_CAP else "")))
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


def judge_text(e: dict) -> str:
    """The entry as a ranker sees it: the same body and fix a reader is given."""
    r = e["raw"]
    body = " ".join(str(_first(r, DISPLAY_BODY) or _longest_unknown(r) or "").split())
    fix = " ".join(str(_first(r, DISPLAY_FIX) or "").split())
    return body[:BODY_CAP] + (" FIX: " + fix[:FIX_CAP] if fix else "")


# Judge more than will be shown. Reordering only the five results already on
# screen can just permute what BM25 liked; in the 2026-09-21 measurement the
# lesson that mattered most for one situation sat at BM25 rank 9 of 10.
RERANK_POOL = int(os.environ.get("RECALL_RERANK_POOL") or 10)

# A candidate written to lose, judged in the SAME call as the real ones.
#
# Pull already knows how to say "nothing here": build_probe_index carries three
# score floors. This side had none, so a nonsense question still came back with
# a confident top hit -- 8.1 against 32.6 for a real question, and no reader can
# see that difference. But a floor on Jev's own number is the one thing this
# project measured and rejected: the ordering is trustworthy and the absolute
# value is not, moving 0.96 -> 0.72 on the same candidates under a thinner
# context. So the floor is a comparison instead of a number. This text is
# grammatical, is shaped like an entry, and is tautological -- it cannot tell
# anyone to do anything, about any subject. Beating it is the lowest bar a real
# answer clears, and because it rides in the same request it moves with
# whatever the judge's scale is doing on THIS question.
CONTROL_LESSON = (
    "A project keeps some of its files in folders, and some of those folders "
    "hold more files than others do. Work tends to happen in the files that "
    "are being worked on. FIX: When you want to know what a file contains, "
    "open that file and look at what it contains."
)

# ...but beating the control is not enough, and measuring said so. Over 27 live
# questions on the real corpus (2026-09-21): on the 16 the corpus answers, the
# best lesson beat the control by 0.46 to 0.93. On the 11 it cannot answer,
# every score collapsed together -- real lessons AND the control all landed
# between 0.03 and 0.08, separated by -0.11 to +0.03. So at the floor, "did
# anything beat the control" is a coin toss between two numbers inside the
# judge's own run-to-run drift.
#
# The gap is the signal, not the winner. The two populations are 0.43 apart and
# never overlap, so any margin between 0.03 and 0.46 scored 16/16 and 11/11.
# 0.15 is the middle of that: three times the widest gap a nonsense question
# produced, three times under the narrowest gap a real one did.
#
# This is still a number, so be clear about which kind. A floor on Jev's raw
# score breaks when the whole query's scale shifts -- the same candidates have
# been measured moving 0.96 -> 0.72 under a thinner context. This margin is
# measured against a control scored in the SAME request, on the SAME question,
# so it rides that shift instead of being broken by it.
ABSTAIN_MARGIN = float(os.environ.get("RECALL_ABSTAIN_MARGIN") or 0.15)


def rerank(query: str, ranked: list, limit: int,
           abstain: bool = True) -> tuple[list, dict | None, bool]:
    """Reorder the top candidates by judged relevance.

    Returns the list unchanged, no scores and no abstention whenever the judge
    cannot answer. A rougher ranking is a far better outcome than a query that
    fails, so every unavailability is a note on stderr and nothing more.

    The third value is True when no candidate cleared the control by
    ABSTAIN_MARGIN -- the judge read them all and none of them answers the
    question.
    """
    pool = ranked[:max(limit, RERANK_POOL)]
    if len(pool) < 2:
        return ranked, None, False   # nothing to reorder, and no call to pay for
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import jev
    except ImportError as exc:
        sys.stderr.write(f"recall: not reranked ({exc}) — showing search order\n")
        return ranked, None, False
    texts = [judge_text(e) for e, _, _ in pool]
    try:
        scores = jev.score(query, texts + [CONTROL_LESSON] if abstain else texts)
    except jev.Unavailable as exc:
        sys.stderr.write(f"recall: not reranked ({exc}) — showing search order\n")
        return ranked, None, False
    control = scores.pop() if abstain else None
    judged = {e["key"]: sc for (e, _, _), sc in zip(pool, scores)}
    # sorted() is stable, so lessons the judge scores equally keep the search
    # order between them instead of being shuffled by an arbitrary tiebreak.
    pool = sorted(pool, key=lambda t: -judged[t[0]["key"]])
    # Ties, and near-ties, go to the control. A lesson that only edges out text
    # about nothing has not answered anyone.
    gave_up = (control is not None and scores
               and max(scores) - control <= ABSTAIN_MARGIN)
    return pool + ranked[len(pool):], judged, gave_up


# Three words is the smallest cut that can remove an instruction rather than
# punctuation or a dangling plural.
MIN_CUT_WORDS = 3


def truncated_fixes(entries: list[dict]) -> list[dict]:
    """Entries whose displayed text stops before the instruction does.

    The push channel caps the body and the "what to do" line at 400 characters
    each. An entry longer than that still fires, still looks delivered, and
    reaches the reader with its operative half missing — often mid-sentence,
    which reads as advice that simply trails off.

    That is a THIRD blind spot, distinct from the two this tool already reports.
    `--unreachable` finds entries push can never fire on. `--hidden` finds
    requirements filed where no channel prints them. This finds entries that
    fire correctly and are cut on the way out.

    Derived by calling the push channel and comparing what it returns against
    the field it drew from, rather than by re-stating its cap here. If push
    changes its cap or its field order tomorrow, this follows.
    """
    push = _push_channel()
    found = []
    for e in entries:
        # Rules never reach the push channel -- they arrive when their skill
        # fires, uncut -- so measuring them against push's cap counts a
        # truncation that cannot happen. Left in and the number moves by 9.
        # Specs never reach it either, and for the same reason.
        if e.get("is_rule") or is_pointer(e):
            continue
        raw = e["raw"]
        parts = [("what to do", push.remedy(raw)), ("body", push.summarize(raw))]
        losses = []
        for label, shown in parts:
            if not shown:
                continue
            # Recover the source field by matching push's own output against it.
            # Asking the layer beats keeping a second copy of its field list.
            full = ""
            for v in raw.values():
                if isinstance(v, str):
                    flat = " ".join(v.split())
                    if flat.startswith(shown) and len(flat) > len(full):
                        full = flat
            tail = full[len(shown):] if full else ""
            # A cut that loses a full stop is not a defect, and a report that
            # flags one gets switched off before it can show a real one. The
            # loss has to be words.
            if len(re.findall(r"[A-Za-z0-9]{2,}", tail)) >= MIN_CUT_WORDS:
                losses.append({"part": label, "cut": len(full) - len(shown),
                               "tail": tail})
        if losses:
            found.append({"id": e["id"], "catalog": e["catalog"], "key": e["key"],
                          "losses": losses,
                          "worst": max(l["cut"] for l in losses)})
    return sorted(found, key=lambda f: f["worst"])


def log_pull(shown: list, mode: str, query: str = "", judged: dict | None = None) -> None:
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
                row = {
                    "ts": ts,
                    "session": session,
                    "event": mode,          # recall-query | recall-id
                    "entry": e["key"],
                    "score": round(score, 1) if isinstance(score, (int, float)) else None,
                    "tokens": hits,
                    "query": query[:200],
                }
                # Present only on a reranked pull, and absent rather than null
                # otherwise, so every row already written keeps its exact shape.
                if judged and e["key"] in judged:
                    row["jev"] = round(judged[e["key"]], 3)
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception:
        pass


def log_abstain(query: str, judged_count: int) -> None:
    """Record that a pull was answered with nothing.

    Carries no `entry`, so every reader that keys on one skips it and no
    existing row changes shape. Without it an abstention is indistinguishable
    from a query nobody ran, and the two have opposite meanings for coverage.
    """
    try:
        SURFACED_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(SURFACED_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "session": os.environ.get("RECALL_SESSION_ID") or "cli",
                "event": "recall-abstain",
                "judged": judged_count,
                "query": query[:200],
            }, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _pull_counts() -> dict:
    """Free-text pulls answered, and pulls answered with nothing.

    Reads the same log `log_pull` and `log_abstain` write, so the counter can
    never disagree with what was actually delivered. Never raises: --stats works
    on a fresh install with no log at all.
    """
    answered, abstained = set(), 0
    try:
        for line in SURFACED_LOG.read_text(errors="replace").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("event") == "recall-abstain":
                abstained += 1
            elif r.get("event") == "recall-query":
                answered.add((r.get("ts", ""), r.get("query", "")))
    except OSError:
        return {}
    total = len(answered) + abstained
    return {"pulls_answered": len(answered), "pulls_abstained": abstained,
            "abstained_pct": round(100 * abstained / total, 1) if total else 0.0}


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
    ap.add_argument("--truncated", action="store_true",
                    help="entries the push channel cuts before the instruction ends")
    ap.add_argument("--rerank", action="store_true",
                    help="reorder results by judged relevance (needs AI_GATEWAY_API_KEY)")
    ap.add_argument("--no-abstain", action="store_true",
                    help="with --rerank, show the results even when none beat the control")
    ap.add_argument("--specs", action="store_true",
                    help="search only the spec corpus, not the failure catalogs")
    ap.add_argument("--hub", action="store_true",
                    help="search only the published hub (specs, plans, playbooks, prototypes)")
    ap.add_argument("--connectors", action="store_true",
                    help="search only connector pointers (Slack, Gmail, meetings)")
    ap.add_argument("--lot", action="store_true",
                    help="the parking lot: list open items, or search only them")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()

    entries = load_entries()
    if a.specs:
        entries = [e for e in entries if e.get("is_spec")]
    if a.hub:
        entries = [e for e in entries if e.get("is_hub")]
    if a.connectors:
        entries = [e for e in entries if e.get("is_connector")]
    if a.lot:
        entries = [e for e in entries if e.get("is_lot")]
    for b in getattr(load_entries, "broken", []):
        sys.stderr.write(
            f"recall: {b['catalog']} catalog did not parse and is EXCLUDED from these "
            f"results — {b['path']}: {b['error']}\n")
    # Before the empty-corpus guard: the lot does not depend on the catalogs,
    # and an empty lot is a clean answer, not a broken install.
    if a.lot and not a.query and not a.id and not a.stats:
        items = lot_order([e["raw"] for e in entries])
        print(json.dumps(items, indent=2) if a.json
              else lot_table(items, lot_tier1_open(items)))
        return 0
    # A freshly installed corpus is EMPTY, and install.sh tells the user to run
    # `recall.py --stats` first. Treating zero entries as "no readable catalogs"
    # made a correct empty state indistinguishable from a broken install, and
    # made the first command a new user runs exit non-zero with a wrong reason.
    if not entries and not getattr(load_entries, "parsed", 0):
        sys.stderr.write(f"recall: no readable catalogs under {CATALOG_DIR}\n")
        return 2

    if a.stats:
        idx = indexed_keys()
        # Rules are not in the injection index by design -- they arrive when
        # their skill fires -- so scoring them here would move a tracked
        # percentage without anything becoming less reachable.
        cat = [e for e in entries if not e.get("is_rule") and not is_pointer(e)]
        rules = [e for e in entries if e.get("is_rule")]
        specs = [e for e in entries if e.get("is_spec")]
        unreachable = [e for e in cat if e["key"] not in idx]
        noprobe = [e for e in unreachable if not e["probe_when"]]
        out = {
            "entries": len(entries),
            "catalog_entries": len(cat),
            "rules_entries": len(rules),
            "spec_entries": len(specs),
            "hub_entries": sum(1 for e in entries if e.get("is_hub")),
            "connector_entries": sum(1 for e in entries if e.get("is_connector")),
            "connector_sources": sorted({e["catalog"] for e in entries
                                         if e.get("is_connector")}),
            "by_catalog": dict(Counter(e["catalog"] for e in entries)),
            "recurring": sum(1 for e in entries if e["recurrences"]),
            "recurrence_events": sum(e["recurrences"] for e in entries),
            "reachable_by_injection": len(cat) - len(unreachable),
            "unreachable_by_injection": len(unreachable),
            # 0.0 on an empty corpus rather than ZeroDivisionError: a fresh
            # install has no entries and --stats is the first thing it is told
            # to run. The old "no readable catalogs" guard returned early and
            # hid this; removing that guard is what surfaced it.
            "unreachable_pct": round(100 * len(unreachable) / len(cat), 1) if cat else 0.0,
            "unreachable_missing_probe_when": len(noprobe),
            "unreachable_despite_probe_when": len(unreachable) - len(noprobe),
            "reachable_by_recall": len(entries),
            # Counted here because a corpus can be 100% reachable and still be
            # failing to deliver: reachability is per ENTRY, this is per
            # STATEMENT inside an entry that is already reachable.
            "hidden_statements": len(hidden_statements(entries)),
            "unreadable_catalogs": getattr(load_entries, "broken", []),
        }
        # A gate that writes somewhere nobody reads is a gate nobody can check.
        # Abstentions land in the same log as pulls, so count them here: a rate
        # near zero means the control is not discriminating, and a rate near one
        # means the corpus stopped answering. Both are visible from one number.
        out.update(_pull_counts())
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

    if a.truncated:
        found = truncated_fixes(entries)
        if a.json:
            print(json.dumps(found, indent=2))
            return 0
        if not found:
            print(f"  {len(entries)} entries, none cut on the way out")
            return 0
        bands = [(1, 100, "trim a clause"), (101, 300, "trim a sentence"),
                 (301, 10**9, "needs rewriting")]
        print(f"{len(found)} of {len(entries)} entries are cut before the instruction "
              f"ends.\nThey fire, they look delivered, and the reader never sees "
              f"the rest.\n")
        for lo, hi, label in bands:
            band = [f for f in found if lo <= f["worst"] <= hi]
            if not band:
                continue
            print(f"  {len(band)} · {label} ({lo}-{hi if hi < 10**9 else '∞'} chars over)")
            # Cheapest first: an entry eighteen characters over is a one-line fix
            # on a lesson that has already cost something.
            for f in band[:a.limit]:
                worst = max(f["losses"], key=lambda l: l["cut"])
                print(f"      +{worst['cut']:<4} {f['id']}  ({worst['part']})")
                print(f"            never shown: …{worst['tail'][:90].strip()}")
            if len(band) > a.limit:
                print(f"      … and {len(band) - a.limit} more")
            print()
        print("Shorten the field, or move the operative sentence to the front of it.")
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
        # Specs are unreachable by injection by design -- no probe_when, and
        # none is wanted -- so listing them here would bury the entries this
        # mode exists to surface under a growing pile working as intended.
        sel = [e for e in entries if not is_pointer(e) and e["key"] not in idx][:a.limit]
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
        # A parked item is a plan with a title and a first move, never a
        # lesson, so it is not a stub for lacking one.
        sel = [e for e in entries if not e.get("is_lot")
               and _empty(e, BODY) and _empty(e, FIX)][:a.limit]
    elif a.query:
        q = " ".join(a.query)
        ranked = bm25(entries, q)
        judged, abstained = None, False
        # The env var exists so a hook or an agent can turn this on for a whole
        # session without editing every call site. Off unless asked either way.
        if a.rerank or (os.environ.get("RECALL_RERANK") or "").strip() not in ("", "0", "false", "no"):
            ranked, judged, abstained = rerank(q, ranked, a.limit,
                                               abstain=not a.no_abstain)
        ranked = ranked[:a.limit]
        if abstained:
            # Nothing is logged as surfaced, because nothing was surfaced.
            log_abstain(q, len(judged or {}))
            if a.json:
                print("[]")
                return 0
            print("  no lesson here answers that")
            print(f"  {len(judged or {})} judged; none beat a control that says nothing.")
            print("  Re-run with --no-abstain to read them anyway.")
            return 0
        log_pull(ranked, "recall-query", q, judged)
        if a.json:
            rows = []
            for e, s, h in ranked:
                row = {"id": e["id"], "catalog": e["catalog"], "score": round(s, 2),
                       "matched": h, "recurrences": e["recurrences"]}
                if judged and e["key"] in judged:
                    row["jev"] = round(judged[e["key"]], 3)
                rows.append(row)
            print(json.dumps(rows, indent=2))
            return 0
        if not ranked:
            print("  no match")
            return 0
        for e, s, h in ranked:
            show(e, s, h, full=a.full, judged=(judged or {}).get(e["key"]))
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
