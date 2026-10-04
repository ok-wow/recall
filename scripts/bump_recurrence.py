#!/usr/bin/env python3
"""Record that a lesson failed again -- the count and the date, in one act.

The corpus has counted recurrences for a long time and dated barely half of
them: 329 events, 193 dated, 184 carrying a bare counter. A bare counter cannot
be placed on a timeline, and the timeline is the whole question. If the lesson
was surfaced BEFORE the failure repeated, the corpus has a heeding problem. If
it was never surfaced, it has a coverage problem. Those need opposite fixes and
an undated bump cannot tell them apart.

Dating was already the written rule. It was followed 57% of the time, which is
what happens when finishing a thing and recording that you finished it are two
separate acts asked of a worker in prose. So this makes them one act: the
counter and the dated note move together or neither moves.

It edits the raw text rather than round-tripping the YAML. A whole-file
yaml.dump would reformat every entry in a 700-entry file, and these catalogs
routinely carry another session's uncommitted work -- a reformat would bury it
beyond review. The span logic below is inherited from a writer that was
hardened through three real shape variants in this corpus; the comments say
which, because each one silently wrote to the wrong place first.

A note also has to name its session. The date says when; the session says
which run, and only that joins a recurrence to what surfaced.jsonl shows was put
in front of that session before it failed. Pass --session, or write
"session <id>" in the note; a note with neither is refused.

Usage:
    bump_recurrence.py [--session SID] <catalog.yaml> <entry-id> <note-file>
    bump_recurrence.py [--session SID] <catalog.yaml> <entry-id> -   # note on stdin
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date
from pathlib import Path

import yaml

DATED = re.compile(r"^recurrence_\d{4}_\d{2}_\d{2}$")
# "session cd406b8e", "session: 0040ce0d-87e5-...": a hex id of 6+ characters.
SESSION_IN_NOTE = re.compile(r"\bsession[:\s]+([0-9a-f]{6}[0-9a-f-]*)\b", re.I)


class DupCatch(yaml.SafeLoader):
    """safe_load tolerates duplicate keys and keeps the last one, so a raced
    append that drops a list marker merges two entries and still parses. The
    structural gate on these catalogs already uses this loader; a writer that
    validates with anything weaker can create exactly what that gate exists to
    catch."""


def _no_dupes(loader, node, deep=False):
    seen = set()
    for k, _ in node.value:
        key = loader.construct_object(k, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", k.start_mark)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


DupCatch.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dupes)


def today() -> str:
    """RECALL_TODAY exists so tests can pin the date. Nothing else sets it."""
    return (os.environ.get("RECALL_TODAY") or date.today().isoformat()).replace("-", "_")


def dump_field(key: str, value: str, indent: str) -> str:
    """The field as PyYAML itself would write it, at the entry's indent.

    Hand-quoting is how a note containing a colon, a backtick or an apostrophe
    breaks the file. PyYAML already knows every one of those rules.
    """
    block = yaml.safe_dump({key: value}, default_flow_style=False,
                           width=100, allow_unicode=True, sort_keys=False)
    return "".join(indent + ln if ln.strip() else ln
                   for ln in block.splitlines(keepends=True))


def entry_span(text: str, eid: str) -> tuple[int, int, int, str]:
    """(start, end, end-of-id-line, field_indent) for the entry whose id is eid.

    Three shapes live in this corpus and all three are load-bearing:

        - id: x        substrate catalogs, id is the item's own first key
          id: x        substrate catalogs, id further down the item
          - id: x      every skill rules.yaml, under a `rules:` mapping

    The third was unreachable until 2026-09-22 because the markers and the item
    boundary were both pinned to column zero. That silently excluded 204 rules
    across okwow-doctrine, -agent-patterns, -harness-discipline, -diagram-style
    and -scrollytelling-craft, so every recurrence on them was hand-written --
    which is how a bare counter with no date gets created, the exact thing this
    script exists to prevent.

    Searched against a leading sentinel newline. Every marker begins with one,
    and the FIRST entry in a file has none before it -- so without the sentinel,
    entry number one is unreachable and the script reports "entry not found"
    for a row that is plainly there.
    """
    hay = "\n" + text
    # id as the item's own first key, at any indent: `<I>- id: eid`
    m = re.search(rf"\n( *)-( +)id: {re.escape(eid)} *(?:\n|$)", hay)
    if m:
        marker_indent = m.group(1)
        field_indent = marker_indent + " " + m.group(2)   # under the `- `
        at, first_key = m.start(), True
    else:
        # id further down the item: `<F>id: eid`, marker is two columns left
        m = re.search(rf"\n( *)id: {re.escape(eid)} *(?:\n|$)", hay)
        if not m:
            raise SystemExit(f"entry not found: {eid}")
        field_indent = m.group(1)
        marker_indent = field_indent[:-2]
        at, first_key = m.start(), False

    item = f"\n{marker_indent}- "
    # With `- id:` the id IS the item marker, so the entry begins there.
    # Searching backwards for the previous item would land inside the entry
    # BEFORE this one and edit that instead -- not hypothetical, the
    # one-change assertion caught it doing exactly that.
    start = at + 1 if first_key else hay.rfind(item, 0, at) + 1
    nxt = hay.find(item, at + 1)             # from AFTER our own marker
    end = nxt + 1 if nxt > 0 else len(hay)
    id_line_end = hay.find("\n", at + 1) + 1
    return start - 1, end - 1, id_line_end - 1, field_indent


def bump(text: str, eid: str, note: str) -> str:
    start, end, id_end, ind = entry_span(text, eid)
    body = text[start:end]
    key = f"recurrence_{today()}"

    # The counter. Absent means this is the first recorded repeat, not an error.
    m = re.search(r"^(\s*-?\s*)recurrences:[ \t]*(\d+)[ \t]*$", body, re.M)
    if m:
        n = int(m.group(2))
        body = body[:m.start()] + f"{m.group(1)}recurrences: {n + 1}" + body[m.end():]
    else:
        body = body[:id_end - start] + f"{ind}recurrences: 1\n" + body[id_end - start:]

    # Same day, second failure. Appending rather than replacing keeps both; a
    # writer that clobbers turns a second event into no event at all.
    if re.search(rf"^{ind}{key}:", body, re.M):
        # The span is one whole list item, so it parses as a one-item list.
        # The span is one whole list item. At a non-zero indent it is not
        # valid YAML on its own, so dedent it to column zero to read it back.
        flat = "".join(ln[len(ind) - 2:] if ln.startswith(ind[:-2]) else ln
                       for ln in body.splitlines(keepends=True)) if ind != "  " else body
        prev = str(yaml.load(flat, DupCatch)[0].get(key, "")).strip()
        note = f"{prev}\n\n{note}" if prev else note
        lo, hi = _field_span(body, key, ind)
        body = body[:lo] + dump_field(key, note, ind) + body[hi:]
    else:
        ins = id_end - start
        body = body[:ins] + dump_field(key, note, ind) + body[ins:]
    return text[:start] + body + text[end:]


def _field_span(body: str, key: str, ind: str) -> tuple[int, int]:
    """Span of `<ind>key: ...` including any continuation lines."""
    k = body.find(f"\n{ind}{key}:")
    if k < 0:
        raise SystemExit(f"field {key!r} vanished mid-edit — refusing")
    k += 1
    rest = body[k:]
    nl = rest.find("\n")
    off = 0
    for line in rest[nl:].splitlines(keepends=True):
        # A sibling field sits at exactly `ind`; a continuation sits deeper.
        if (line.startswith(ind) and not line.startswith(ind + " ")
                and ":" in line and off):
            break
        off += len(line)
    return k, k + nl + off


def entries(doc) -> list:
    """The list of entries, whichever shape the file wraps them in.

    A substrate catalog IS the list. A skill rules.yaml is a mapping with one
    list under `rules:`. Guarding on the shape here keeps every assertion below
    -- entry count, exactly-one-change, counter+1 -- working for both.
    """
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        lists = [v for v in doc.values()
                 if isinstance(v, list) and v and isinstance(v[0], dict)]
        if len(lists) == 1:
            return lists[0]
        raise SystemExit(f"cannot find one entry list in a mapping with "
                         f"{len(lists)} candidate(s) — refusing")
    raise SystemExit(f"unsupported top-level {type(doc).__name__} — refusing")


def main() -> int:
    args = sys.argv[1:]
    session = ""
    if "--session" in args:
        i = args.index("--session")
        session = args[i + 1].strip() if i + 1 < len(args) else ""
        del args[i:i + 2]
    if len(args) != 3:
        raise SystemExit(__doc__.strip().splitlines()[-2].strip())
    path, eid, notefile = args
    note = " ".join((sys.stdin.read() if notefile == "-"
                     else Path(notefile).read_text()).split())
    if not note:
        raise SystemExit("a recurrence with no note is a counter again — refusing")
    if session and session.lower() not in note.lower():
        note = f"{note} (session {session})"
    if not session and not SESSION_IN_NOTE.search(note):
        raise SystemExit("a recurrence with no session id cannot be joined to what was "
                         "surfaced first — pass --session <id> or write 'session <id>' "
                         "in the note — refusing")

    p = Path(path)
    text = p.read_text()
    before = entries(yaml.load(text, DupCatch))
    patched = bump(text, eid, note)
    after = entries(yaml.load(patched, DupCatch))   # rejects a duplicate key

    # Everything below is the guard, and the guard is the point. A surgical
    # edit that writes to the wrong entry looks exactly like a correct one
    # until something re-reads the file.
    if len(before) != len(after):
        raise SystemExit(f"entry count {len(before)} -> {len(after)} — refusing")
    changed = [(b.get("id"), k) for b, a in zip(before, after) if b != a
               for k in set(b) | set(a) if b.get(k) != a.get(k)]
    key = f"recurrence_{today()}"
    allowed = {(eid, "recurrences"), (eid, key)}
    if set(changed) - allowed:
        raise SystemExit(f"unexpected changes {sorted(set(changed) - allowed)} — refusing")
    idx = [e.get("id") for e in after].index(eid)
    b4 = before[idx].get("recurrences")
    b4 = b4 if isinstance(b4, int) else 0
    if after[idx].get("recurrences") != b4 + 1:
        raise SystemExit("counter did not advance by exactly one — refusing")
    if note.split()[-1] not in " ".join(str(after[idx][key]).split()):
        raise SystemExit("note did not round-trip — refusing")

    p.write_text(patched)
    print(f"ok  {eid}  recurrences {b4} -> {b4 + 1}  {key} written "
          f"({len(note)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
