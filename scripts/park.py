#!/usr/bin/env python3
"""park — put work you decided to do later where recall will find it again.

"Do this later" used to end up as a line in a handoff, and nothing ever showed
a handoff line again. The owner parks work in many sessions and relies on the
system to keep it, so this is a store any session can write and recall
searches: one JSON file per item under $RECALL_HOME/lot/items.

    park.py add --title "..." [--body TEXT|-] [--tier 1|2|3|inbox] [--theme T] ...
    park.py list [--tier N|inbox] [--lane L] [--effort E] [--sort tier|lane|effort] ...
    park.py show ID
    park.py set ID [--tier N|inbox] [--lane L] [--effort E] [--status S] [--theme T] ...
    park.py done ID [--note TEXT]
    park.py kill ID --why TEXT
    park.py import FILE.jsonl
    park.py export --to DIR
    park.py check HANDOFF.md

Every subcommand takes --json. Exit codes: 0 ok; 1 not found, a failed check,
or an import with rejected lines; 2 bad input; 3 an open item already says
this; 4 tier 1 is full; 5 the store could not be written.

Standard library only, except export, which writes YAML front matter.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:          # no flock on Windows; writes stay atomic, just unserialised
    fcntl = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recall as R  # noqa: E402  -- the lot's path, reader, order and table live there

THEMES = ("prompt", "router", "surface", "template", "delegation", "eval", "performance",
          "canvas", "decks", "connectors", "memory", "dev-environment", "docs", "process",
          "security", "product", "other")
STATUSES = R.LOT_OPEN + ("done", "killed")
SOURCE_KINDS = ("session", "handoff", "file", "owner")
ID_MAX, TITLE_MAX, QUOTE_MAX = 60, 160, 200
ID_SHAPE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
OK, NOT_FOUND, BAD, DUPLICATE, TIER1_FULL, WRITE_FAILED = 0, 1, 2, 3, 4, 5

# Four fifths of the shorter side's content words shared, on the title alone or
# on title plus body, is the same item; so are two titles with one slug. Stop
# words do not count ("a retry to the queue consumer" vs "a timeout to the queue
# consumer" is 3 of 4 content words: two tasks). A false refusal drops a task;
# a missed match leaves two items that drift apart, and --new covers a real miss.
DUPLICATE_OVERLAP = 0.8
# recall's tokenizer does not stem, so "retries" and "retry" never met. A crude
# fold is enough here: both sides get the same fold, so it only has to agree
# with itself, not with English.
FOLD = (("ies", "y"), ("ing", ""), ("ed", ""), ("s", ""))


class Refused(Exception):
    def __init__(self, code: int, message: str, payload: dict | None = None, detail: str = ""):
        super().__init__(message)
        self.code, self.message, self.payload, self.detail = code, message, payload or {}, detail


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def one_line(text) -> str:
    return " ".join(str(text or "").split())


def is_open(item: dict) -> bool:
    return str(item.get("status") or "parked") in R.LOT_OPEN


def parse_tier(value):
    if value is None or str(value).lower() in ("", "inbox", "none", "null"):
        return None
    if str(value) in ("1", "2", "3") and not isinstance(value, bool):
        return int(value)
    raise Refused(BAD, f"tier must be 1, 2, 3 or inbox, not {value!r}")


def tier_text(t) -> str:
    return f"tier {t}" if t else "the inbox"


# ------------------------------------------------------------------ the item --

def make_item(f: dict, session: str | None) -> dict:
    """One validated item from caller fields. add and import both come here,
    so a line in a JSONL file obeys exactly the rules a command line does."""
    title = one_line(f.get("title"))
    if not title:
        raise Refused(BAD, "a title is required")
    if len(title) > TITLE_MAX:
        raise Refused(BAD, f"the title is {len(title)} characters; keep it under {TITLE_MAX} "
                           "and put the rest in the body")
    iid = f.get("id") or None
    if iid is not None and (not ID_SHAPE.match(str(iid)) or len(str(iid)) > ID_MAX):
        raise Refused(BAD, f"an id is kebab-case and at most {ID_MAX} characters, not {iid!r}")
    theme = f.get("theme") or "other"
    if theme not in THEMES:
        raise Refused(BAD, f"theme must be one of: {', '.join(THEMES)}")
    status = f.get("status") or "parked"
    if status not in STATUSES:
        raise Refused(BAD, f"status must be one of: {', '.join(STATUSES)}")
    src = f.get("source") if isinstance(f.get("source"), dict) else {}
    kind = src.get("kind") or "session"
    if kind not in SOURCE_KINDS:
        raise Refused(BAD, f"source kind must be one of: {', '.join(SOURCE_KINDS)}")
    quote = one_line(src.get("quote")) or None
    if quote and len(quote) > QUOTE_MAX:
        quote = quote[:QUOTE_MAX - 1] + "…"
    links = f.get("links") if isinstance(f.get("links"), dict) else {}
    prs, revisit = links.get("prs") or [], f.get("revisit_when") or []
    revisit = [revisit] if isinstance(revisit, str) else revisit
    if not isinstance(prs, list) or not isinstance(revisit, list):
        raise Refused(BAD, "links.prs and revisit_when are lists")
    stamp = now()
    # An import may carry the date the work was first deferred; its age is real.
    created = str(f.get("created") or "")
    lane, effort = f.get("lane") or None, f.get("effort") or None
    if lane is not None and lane not in R.LOT_LANES:
        raise Refused(BAD, f"lane must be one of: {', '.join(R.LOT_LANES)}")
    if effort is not None and effort not in R.LOT_EFFORTS:
        raise Refused(BAD, f"effort must be one of: {', '.join(R.LOT_EFFORTS)}")
    return {
        "id": iid, "title": title, "body": str(f.get("body") or "").strip(),
        "first_move": one_line(f.get("first_move")) or None,
        "theme": theme, "tier": parse_tier(f.get("tier")), "lane": lane, "effort": effort,
        "status": status,
        "owner_said": f.get("owner_said") is True,
        "source": {"session_id": src.get("session_id") or session, "kind": kind,
                   "ref": src.get("ref") or None, "quote": quote},
        "links": {"linear": links.get("linear") or None, "prs": [str(p) for p in prs],
                  "spec": links.get("spec") or None},
        "revisit_when": [one_line(c) for c in revisit if one_line(c)],
        "created": created if re.match(r"^\d{4}-\d{2}-\d{2}", created) else stamp,
        "last_touched": stamp, "history": [],
    }


def slugify(title: str) -> str:
    flat = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "-", flat.lower()).strip("-") or "item"
    if len(base) > ID_MAX:
        cut = base[:ID_MAX + 1]
        # Whole words when there are any; a 61-character word is cut hard.
        base = (cut.rsplit("-", 1)[0] if "-" in cut else cut[:ID_MAX]).strip("-")
    return base


def new_id(title: str, taken: set[str]) -> str:
    base = slugify(title)
    iid, n = base, 2
    while iid in taken:
        tail = f"-{n}"
        iid = base[:ID_MAX - len(tail)].rstrip("-") + tail
        n += 1
    return iid


def record(item: dict, change: str, session: str | None) -> None:
    """Every change is appended, never rewritten: the history is how a later
    session learns who moved an item and why."""
    item["last_touched"] = now()
    item.setdefault("history", []).append(
        {"at": item["last_touched"], "session_id": session, "change": change})


def touched_by(item: dict, session: str) -> bool:
    src = item.get("source") if isinstance(item.get("source"), dict) else {}
    return src.get("session_id") == session or any(
        isinstance(h, dict) and h.get("session_id") == session for h in item.get("history") or [])


# --------------------------------------------------------- duplicates, cap --

def words(text: str) -> set[str]:
    out = set()
    for w in R.tokenize(text):
        for suffix, repl in FOLD:
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                w = w[: -len(suffix)] + repl
                break
        out.add(w)
    return out


def overlap(a: set, b: set) -> float:
    """Share of the shorter side's words that the other side also has."""
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def duplicate_of(item: dict, items: list[dict]):
    """The open item this one repeats, and how much they share, or None."""
    title, full = words(item["title"]), words(f"{item['title']} {item['body']}")
    best = None
    for other in items:
        if other["id"] == item.get("id") or not is_open(other):
            continue
        other_title = str(other.get("title") or "")
        score = max(overlap(title, words(other_title)),
                    overlap(full, words(f"{other_title} {other.get('body') or ''}")))
        if slugify(item["title"]) == slugify(other_title):
            score = 1.0
        if score >= DUPLICATE_OVERLAP and (best is None or score > best[1]):
            best = (other, score)
    return best


def check_tier1(item: dict, items: list[dict]) -> None:
    """Refuse a sixth open tier 1 item.

    The cap is the feature. A "do next" list that grows without limit stops
    meaning anything, so there is no flag around this: the caller demotes one
    of the five first, which is the choice the cap exists to force.
    """
    if R.lot_tier(item) != 1 or not is_open(item):
        return
    holders = R.lot_order([i for i in items if i["id"] != item.get("id")
                           and R.lot_tier(i) == 1 and is_open(i)])
    if len(holders) >= R.LOT_TIER1_CAP:
        raise Refused(
            TIER1_FULL, f"tier 1 is full ({len(holders)} of {R.LOT_TIER1_CAP}). "
                        "Demote one of these first, then try again:",
            {"cap": R.LOT_TIER1_CAP, "tier1": holders},
            R.lot_table(holders) + f"\n\n  park.py set {holders[-1]['id']} --tier 2")


# --------------------------------------------------------------- the store --

def load_all() -> list[dict]:
    broken: list[dict] = []
    items = R.read_lot(broken)
    for b in broken:
        sys.stderr.write(f"park: skipped {b['path']}: {b['error']}\n")
    return items


def find(iid: str, items: list[dict]) -> dict:
    for i in items:
        if i["id"] == iid:
            return i
    near = [i["id"] for i in items if iid.lower() in i["id"]][:5]
    raise Refused(NOT_FOUND, f"no parked item '{iid}'"
                  + (f" — did you mean: {', '.join(near)}" if near else ""))


@contextmanager
def locked():
    """One writer at a time. The duplicate check and the tier 1 cap both read
    the whole lot before writing; two sessions interleaving there would get
    two copies of one item, or six "do next" items."""
    R.LOT_DIR.mkdir(parents=True, exist_ok=True)
    with open(R.LOT_DIR / ".lock", "a") as fh:
        if fcntl:
            fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def write_atomic(path: Path, text: str) -> None:
    """Write beside the target, then rename over it. A reader sees the old
    file or the new one, never half of either, and a failed write leaves
    nothing behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


WRITTEN: list[Path] = []  # committed together once the command succeeds


def save(item: dict) -> Path:
    path = R.LOT_DIR / f"{item['id']}.json"
    write_atomic(path, json.dumps(item, indent=2, ensure_ascii=False) + "\n")
    WRITTEN.append(path)
    return path


def park_new(item: dict, items: list[dict], session, change: str,
             allow_duplicate: bool = False) -> dict:
    # Stems of unreadable files count as taken: a corrupt file is somebody's
    # data, and a new item must never be written over it.
    taken = {i["id"] for i in items} | {p.stem for p in R.LOT_DIR.glob("*.json")}
    if item["id"] and item["id"] in taken:
        raise Refused(BAD, f"the id '{item['id']}' is already taken")
    if not allow_duplicate:
        dup = duplicate_of(item, items)
        if dup:
            other, score = dup
            raise Refused(
                DUPLICATE, f"an open item already says this ({round(100 * score)}% the same "
                           f"words): {other['id']}",
                {"duplicate_of": other["id"], "overlap": round(score, 2), "item": other},
                "\n".join(R.lot_lines(other))
                + f"\n\n  same task:       park.py set {other['id']} ...\n"
                "  different task:  add it again with --new")
    check_tier1(item, items)
    item["id"] = item["id"] or new_id(item["title"], taken)
    record(item, f"{change} in {tier_text(item['tier'])}", session)
    save(item)
    return item


def merge(into: dict, new: dict, session, change: str) -> None:
    """Fold a repeat into the item it repeats: keep what the repeat adds,
    change nothing the item already says, and note where the repeat came from."""
    into.setdefault("revisit_when", [])
    into["revisit_when"] += [c for c in new["revisit_when"] if c not in into["revisit_when"]]
    links = into.setdefault("links", {})
    links["prs"] = (links.get("prs") or []) + [p for p in new["links"]["prs"]
                                              if p not in (links.get("prs") or [])]
    for k in ("linear", "spec"):
        links[k] = links.get(k) or new["links"][k]
    into["first_move"] = into.get("first_move") or new["first_move"]
    into["owner_said"] = bool(into.get("owner_said")) or new["owner_said"]
    src = new["source"]
    where = " ".join(str(x) for x in (src["kind"], src["ref"]) if x)
    record(into, f"{change}: \"{new['title']}\" ({where})"
                 + (f' — "{src["quote"]}"' if src["quote"] else ""), session)


# ---------------------------------------------------------------- commands --

def emit(a, data, text: str) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False) if a.json else text)


def cmd_add(a, session) -> int:
    body = sys.stdin.read() if a.body == "-" else a.body
    item = make_item({
        "id": a.id, "title": a.title, "body": body, "first_move": a.first_move,
        "theme": a.theme, "tier": a.tier, "lane": a.lane, "effort": a.effort,
        "owner_said": a.owner_said,
        "source": {"session_id": session, "kind": a.source_kind, "ref": a.source_ref,
                   "quote": a.quote},
        "links": {"linear": a.linear, "prs": a.pr or [], "spec": a.spec},
        "revisit_when": a.revisit_when or []}, session)
    with locked():
        item = park_new(item, load_all(), session, "parked", allow_duplicate=a.new)
    emit(a, item, f"parked {item['id']} in {tier_text(item['tier'])} · {item['theme']}\n"
                  f"  {R.LOT_DIR / (item['id'] + '.json')}")
    return OK


def cmd_import(a, session) -> int:
    path = Path(a.file).expanduser()
    if not path.is_file():
        raise Refused(BAD, f"no such file: {path}")
    added, merged, problems = [], [], []
    with locked():
        items = load_all()
        # One bad line is reported and skipped. Stopping would leave the file
        # half imported with no record of where it stopped.
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                fields = json.loads(line)
                if not isinstance(fields, dict):
                    raise Refused(BAD, "a line must be one JSON object")
                item = make_item(fields, session)
                dup = duplicate_of(item, items)
                if dup:
                    merge(dup[0], item, session, f"merged a repeat from {path.name} line {n}")
                    save(dup[0])
                    merged.append(dup[0]["id"])
                    continue
                items.append(park_new(item, items, session, f"parked from {path.name} line {n}",
                                      allow_duplicate=True))
                added.append(item["id"])
            except json.JSONDecodeError as exc:
                problems.append({"line": n, "error": f"not valid JSON ({exc.msg})"})
            except Refused as r:
                problems.append({"line": n, "error": r.message})
    summary = (f"import: {len(added)} added, {len(merged)} merged as duplicates, "
               f"{len(problems)} rejected")
    emit(a, {"added": len(added), "merged": len(merged), "rejected": len(problems),
             "added_ids": added, "merged_into": merged, "problems": problems},
         "\n".join([summary] + [f"  line {p['line']}: {p['error']}" for p in problems]))
    return NOT_FOUND if problems else OK


def cmd_list(a, session) -> int:
    items = load_all()
    sel = [i for i in items if (str(i.get("status") or "parked") == a.status if a.status
                                else is_open(i))]
    if a.tier:
        t = parse_tier(a.tier)
        sel = [i for i in sel if R.lot_tier(i) == t]
    if a.session:
        sel = [i for i in sel if touched_by(i, a.session)]
    if a.theme:
        sel = [i for i in sel if i.get("theme") == a.theme]
    if a.owner_said:
        sel = [i for i in sel if i.get("owner_said")]
    if a.lane:
        sel = [i for i in sel if i.get("lane") == a.lane]
    if a.effort:
        sel = [i for i in sel if i.get("effort") == a.effort]
    sel = R.lot_order(sel)
    # A stable sort over the tier order: within one lane or effort, the most
    # urgent still comes first. Items without the field go last.
    order = {"lane": R.LOT_LANES, "effort": R.LOT_EFFORTS}.get(a.sort)
    if order:
        sel.sort(key=lambda i: order.index(i[a.sort]) if i.get(a.sort) in order else len(order))
    emit(a, sel, R.lot_table(sel, R.lot_tier1_open(items)))
    return OK


def cmd_show(a, session) -> int:
    item = find(a.item_id, load_all())
    if a.json:
        emit(a, item, "")
    else:
        print("\n".join([item["id"]] + R.lot_lines(item, full=True)))
    return OK


def cmd_set(a, session) -> int:
    if a.status == "killed":
        raise Refused(BAD, "killing an item needs a reason: park.py kill ID --why \"...\"")
    with locked():
        items = load_all()
        item = find(a.item_id, items)
        was_tier1 = R.lot_tier(item) == 1 and is_open(item)
        changes = []

        def put(key, value, note):
            if value is not None and item.get(key) != value:
                changes.append(note(item.get(key), value))
                item[key] = value

        if a.tier:
            tier = parse_tier(a.tier)
            if tier != R.lot_tier(item):
                changes.append(f"{tier_text(R.lot_tier(item))} -> {tier_text(tier)}")
                item["tier"] = tier
        put("status", a.status, lambda o, v: f"status {o} -> {v}")
        put("theme", a.theme, lambda o, v: f"theme {o} -> {v}")
        put("lane", a.lane, lambda o, v: f"lane {o or 'none'} -> {v}")
        put("effort", a.effort, lambda o, v: f"effort {o or 'none'} -> {v}")
        if a.title is not None:
            title = one_line(a.title)
            if not title or len(title) > TITLE_MAX:
                raise Refused(BAD, f"a title is one line of 1 to {TITLE_MAX} characters")
            put("title", title, lambda o, v: f"title was \"{o}\"")
        body = sys.stdin.read() if a.body == "-" else a.body
        put("body", body.strip() if body is not None else None, lambda o, v: "body rewritten")
        put("first_move", one_line(a.first_move) if a.first_move else None,
            lambda o, v: f"first move -> {v}")
        if a.owner_said and not item.get("owner_said"):
            item["owner_said"] = True
            changes.append("the owner asked for it")
        links = item.setdefault("links", {"linear": None, "prs": [], "spec": None})
        for key, value in (("linear", a.linear), ("spec", a.spec)):
            if value and links.get(key) != value:
                changes.append(f"{key} -> {value}")
                links[key] = value
        for pr in a.pr or []:
            if pr not in (links.get("prs") or []):
                links["prs"] = (links.get("prs") or []) + [pr]
                changes.append(f"pr added {pr}")
        for c in [one_line(c) for c in a.revisit_when or [] if one_line(c)]:
            if c not in item.setdefault("revisit_when", []):
                item["revisit_when"].append(c)
                changes.append(f"revisit when: {c}")
        if not changes:
            emit(a, item, f"no change to {item['id']}")
            return OK
        # Only a move INTO an open tier 1 is capped, so a hand-edited lot that
        # is already over the cap can still have its other fields edited.
        if not was_tier1:
            check_tier1(item, items)
        record(item, "; ".join(changes), session)
        save(item)
    emit(a, item, f"updated {item['id']}: " + "; ".join(changes))
    return OK


def close(a, session, status: str, change: str) -> int:
    with locked():
        item = find(a.item_id, load_all())
        if item.get("status") == status:
            emit(a, item, f"{item['id']} is already {status}")
            return OK
        item["status"] = status
        record(item, change, session)
        save(item)
    emit(a, item, f"{status} {item['id']}")
    return OK


def cmd_done(a, session) -> int:
    return close(a, session, "done", "done" + (f": {one_line(a.note)}" if a.note else ""))


def cmd_kill(a, session) -> int:
    why = one_line(a.why)
    if not why:
        raise Refused(BAD, "say why it is being killed: --why \"...\"")
    return close(a, session, "killed", f"killed: {why}")


def cmd_export(a, session) -> int:
    """One markdown file per open item, so the lot can be committed to a git
    repo as a backup. Files for items no longer open are reported, never
    deleted: this writes into a directory it does not own."""
    import yaml
    out = Path(a.to).expanduser()
    items = R.lot_order([i for i in load_all() if is_open(i)])
    written = []
    for i in items:
        links = i.get("links") if isinstance(i.get("links"), dict) else {}
        front = {"id": i["id"], "title": i.get("title"), "created": i.get("created"),
                 "last_touched": i.get("last_touched"), "priority_tier": R.lot_tier(i),
                 "status": i.get("status") or "parked", "theme": i.get("theme") or "other",
                 "linear_ticket": links.get("linear"),
                 "revisit_triggers": list(i.get("revisit_when") or [])}
        src = i.get("source") if isinstance(i.get("source"), dict) else {}
        where = " · ".join(x for x in (f"session {src['session_id']}" if src.get("session_id") else "",
                                        " ".join(str(y) for y in (src.get("kind"), src.get("ref")) if y)) if x)
        text = ("---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True) + "---\n\n"
                + f"# {i.get('title')}\n\n" + (f"{i['body']}\n\n" if i.get("body") else "")
                + (f"First move: {i['first_move']}\n\n" if i.get("first_move") else "")
                + (f"Source: {where}" + (f' — "{src["quote"]}"' if src.get("quote") else "") + "\n"
                   if where or src.get("quote") else ""))
        path = out / f"{i['id']}.md"
        write_atomic(path, text)
        written.append(str(path))
    open_ids = {i["id"] for i in items}
    stale = sorted(p.name for p in out.glob("*.md") if p.stem not in open_ids) if out.exists() else []
    emit(a, {"written": written, "stale": stale},
         f"exported {len(written)} open item(s) to {out}"
         + (f"\n  {len(stale)} file(s) there are for items no longer open, left in place: "
            + ", ".join(stale) if stale else ""))
    return OK


# A heading that starts with a word handoffs use for work left over, after an
# optional "3." or "5b)" number. "Decisions" and "Done" are not among them.
PARKED = re.compile(
    r"^(?:\d+[a-z]?[.)]\s*)?(?:parked|parking|deferred|backlog|follow[- ]?ups?|later|not done"
    r"|open items|next session)\b", re.I)
BULLET = re.compile(r"^( *)(?:[-*+]|\d+[.)])\s+")
LOT_REF = re.compile(r"\blot:([a-z0-9]+(?:-[a-z0-9]+)*)")


def parked_lines(text: str) -> tuple[int, list[tuple[int, str]]]:
    """Every bullet and table row under a heading that starts with Parked,
    Deferred, Backlog, Follow-ups and the like, with the line it starts on. A bullet's nested lines belong to
    it, so a reference anywhere in the block counts."""
    sections, found, level, fenced, current = 0, [], None, False, None
    for n, line in enumerate(text.splitlines(), 1):
        if R.MD_FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        h = R.MD_HEADING.match(line)
        if h:
            current = None
            if level is not None and len(h.group(1)) <= level:
                level = None
            if level is None and PARKED.match(h.group(2)):
                level, sections = len(h.group(1)), sections + 1
            continue
        if level is None:
            continue
        if R.MD_ROW.match(line):
            current = None
            if R.MD_RULE.match(line):
                if found and found[-1][2] == "row" and found[-1][0] == n - 1:
                    found.pop()          # the row above a rule is the header
                continue
            found.append([n, line.strip(), "row"])
            continue
        b = BULLET.match(line)
        if b and (current is None or len(b.group(1)) < 2):
            current = [n, line.strip(), "bullet"]
            found.append(current)
        elif current is not None and line.strip() and (b or line[:1] in (" ", "\t")):
            current[1] += " " + line.strip()
        elif line.strip():
            current = None               # a plain paragraph ends the bullet
    return sections, [(n, t) for n, t, _ in found]


def cmd_check(a, session) -> int:
    """Fail when a parked line in a handoff points at nothing in the lot.

    A handoff that parks work in prose is the exact place parked work used to
    disappear. This makes each such line carry lot:<id>, so the handoff is a
    view of the lot rather than a second, unsearchable copy of it.
    """
    path = Path(a.handoff).expanduser()
    if not path.is_file():
        raise Refused(BAD, f"no such file: {path}")
    known = {i["id"] for i in load_all()}
    sections, lines = parked_lines(path.read_text())
    problems = []
    for n, text in lines:
        refs = LOT_REF.findall(text)
        unknown = [r for r in refs if r not in known]
        if not refs:
            problems.append({"line": n, "text": text, "error": "names no lot:<id>"})
        elif unknown:
            problems.append({"line": n, "text": text,
                             "error": "not in the lot: " + ", ".join(f"lot:{u}" for u in unknown)})
    data = {"ok": not problems, "sections": sections, "lines": len(lines), "problems": problems}
    if not sections:
        emit(a, data, f"check: no Parked or Deferred section in {path.name}, nothing to check")
    elif not problems:
        emit(a, data, f"check: {len(lines)} parked line(s) in {path.name}, each names an item in the lot")
    else:
        emit(a, data, "\n".join(
            [f"check: {len(problems)} of {len(lines)} parked line(s) in {path} are not in the lot:"]
            + [f"  line {p['line']}: {p['text'][:110]}\n      {p['error']}" for p in problems]
            + ["", "  Park each one with park.py add, then write lot:<id> on its line."]))
    return NOT_FOUND if problems else OK


# --------------------------------------------------------------------- main --

def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    actor = argparse.ArgumentParser(add_help=False)
    actor.add_argument("--session", help="the session making this change "
                                         "(default: $RECALL_SESSION_ID)")
    ap = argparse.ArgumentParser(prog="park", description="Park work for later, where recall will find it.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def fields(p):
        p.add_argument("--body", help="what it is and why it matters; - reads stdin")
        p.add_argument("--theme", choices=THEMES)
        p.add_argument("--tier", choices=("1", "2", "3", "inbox"),
                       help="1 do next, 2 soon, 3 someday, inbox not sorted yet")
        p.add_argument("--lane", choices=R.LOT_LANES,
                       help="who picks it up: overnight runner, the owner, or a spec first")
        p.add_argument("--effort", choices=R.LOT_EFFORTS, help="size in AI time: S, M or L")
        p.add_argument("--first-move", help="the first concrete step")
        p.add_argument("--owner-said", action="store_true",
                       help="the owner asked for this in their own words")
        p.add_argument("--linear", help="Linear ticket id")
        p.add_argument("--pr", action="append", help="a pull request link (repeatable)")
        p.add_argument("--spec", help="path to the spec that covers it")
        p.add_argument("--revisit-when", action="append",
                       help="a plain condition that would raise its priority (repeatable)")

    p = sub.add_parser("add", parents=[common, actor], help="park a new item")
    p.add_argument("--title", required=True, help="one line, plain words")
    p.add_argument("--id", help="kebab-case id; made from the title when not given")
    fields(p)
    p.add_argument("--source-kind", choices=SOURCE_KINDS)
    p.add_argument("--source-ref", help="the handoff, file or path it came from")
    p.add_argument("--quote", help="the words that asked for it, up to 200 characters")
    p.add_argument("--new", action="store_true", help="skip the duplicate check")
    p.set_defaults(run=cmd_add)

    p = sub.add_parser("import", parents=[common, actor], help="park one item per JSONL line")
    p.add_argument("file")
    p.set_defaults(run=cmd_import)

    p = sub.add_parser("list", parents=[common], help="open items, most urgent first")
    p.add_argument("--tier", choices=("1", "2", "3", "inbox"))
    p.add_argument("--session", help="items this session parked or changed")
    p.add_argument("--theme", choices=THEMES)
    p.add_argument("--status", choices=STATUSES, help="default: parked and in-progress")
    p.add_argument("--owner-said", action="store_true")
    p.add_argument("--lane", choices=R.LOT_LANES)
    p.add_argument("--effort", choices=R.LOT_EFFORTS)
    p.add_argument("--sort", choices=("tier", "lane", "effort"), default="tier",
                   help="group by lane or effort; tier order holds within a group")
    p.set_defaults(run=cmd_list)

    p = sub.add_parser("show", parents=[common], help="one item in full")
    p.add_argument("item_id")
    p.set_defaults(run=cmd_show)

    p = sub.add_parser("set", parents=[common, actor], help="change an item")
    p.add_argument("item_id")
    p.add_argument("--title")
    p.add_argument("--status", choices=STATUSES)
    fields(p)
    p.set_defaults(run=cmd_set)

    p = sub.add_parser("done", parents=[common, actor], help="mark an item finished")
    p.add_argument("item_id")
    p.add_argument("--note")
    p.set_defaults(run=cmd_done)

    p = sub.add_parser("kill", parents=[common, actor], help="drop an item, with the reason")
    p.add_argument("item_id")
    p.add_argument("--why", required=True)
    p.set_defaults(run=cmd_kill)

    p = sub.add_parser("export", parents=[common], help="write open items as markdown files")
    p.add_argument("--to", required=True)
    p.set_defaults(run=cmd_export)

    p = sub.add_parser("check", parents=[common],
                       help="fail when a handoff's Parked section names nothing in the lot")
    p.add_argument("handoff")
    p.set_defaults(run=cmd_check)

    a = ap.parse_args(argv)
    session = (getattr(a, "session", None) if a.cmd != "list" else None) \
        or os.environ.get("RECALL_SESSION_ID") or None
    try:
        rc = a.run(a, session)
        if rc == 0 and WRITTEN:
            from store_commit import report
            report(WRITTEN, f"lot: {a.cmd} {', '.join(p.stem for p in WRITTEN)[:120]}")
        return rc
    except Refused as r:
        if a.json:
            print(json.dumps({"error": r.message, **r.payload}, indent=2, ensure_ascii=False))
        else:
            sys.stderr.write(f"park: {r.message}\n" + (f"{r.detail}\n" if r.detail else ""))
        return r.code
    except OSError as exc:
        sys.stderr.write(f"park: could not write the lot at {R.LOT_DIR}: {exc}\n")
        return WRITE_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
