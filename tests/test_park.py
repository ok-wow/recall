#!/usr/bin/env python3
"""The parking lot's capture side -- scripts/park.py.

"Do this later" used to be a line in a handoff, and nothing ever showed a
parked line again. The owner relies on the system to keep those lines, so the
store has to hold under the ways sessions actually use it: many at once, the
same idea noticed twice, a list where everything wants to be urgent, a file
someone half-wrote.

Every run gets its own temp store through RECALL_LOT_DIR, and HOME and
RECALL_HOME point into the same temp dir, so a path that forgot its override
lands in the sandbox instead of the owner's real lot.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(os.environ.get("RECALL_SKILL_DIR") or REPO / "scripts")
PARK = SCRIPT_DIR / "park.py"
ID_SHAPE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def env_for(d: Path, lot: Path | None = None, **extra: str) -> dict:
    # Drop every inherited RECALL_/OKWOW_ variable first: a parent session
    # that exported OKWOW_LOT_DIR or RECALL_RERANK would otherwise steer
    # these runs at the real store or a network judge.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("RECALL_", "OKWOW_"))}
    env.update({"HOME": str(d / "home"),
                "RECALL_HOME": str(d / "recall-home"),
                "RECALL_LOT_DIR": str(lot or d / "lot"),
                "RECALL_SPECS_DIR": str(d / "no-specs"),
                "RECALL_SESSION_ID": "sess-test"})
    env.update(extra)
    return env


def park(d: Path, *args: str, stdin: str | None = None, env: dict | None = None,
         lot: Path | None = None) -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(PARK), *args], input=stdin,
                       capture_output=True, text=True, env=env or env_for(d, lot))
    return p.returncode, p.stdout + p.stderr


def park_json(d: Path, *args: str, stdin: str | None = None, env: dict | None = None):
    """stdout parsed as JSON; stderr is for warnings and never mixed in."""
    p = subprocess.run([sys.executable, str(PARK), *args, "--json"], input=stdin,
                       capture_output=True, text=True, env=env or env_for(d))
    try:
        return p.returncode, json.loads(p.stdout)
    except Exception:
        return p.returncode, p.stdout + p.stderr


def item(iid: str, **kw) -> dict:
    """A whole item, as park.py writes one. The fields are the contract."""
    base = {"id": iid, "title": iid.replace("-", " "), "body": "", "first_move": None,
            "theme": "other", "tier": None, "status": "parked", "owner_said": False,
            "source": {"session_id": None, "kind": "session", "ref": None, "quote": None},
            "links": {"linear": None, "prs": [], "spec": None}, "revisit_when": [],
            "created": "2026-09-01T00:00:00Z", "last_touched": "2026-09-01T00:00:00Z",
            "history": [{"at": "2026-09-01T00:00:00Z", "session_id": None, "change": "parked"}]}
    base.update(kw)
    return base


def write(lot: Path, it: dict) -> None:
    lot.mkdir(parents=True, exist_ok=True)
    (lot / f"{it['id']}.json").write_text(json.dumps(it, indent=2))


def main() -> int:
    fails, ran = [], [0]

    def check(name, cond, detail=""):
        ran[0] += 1
        print(f"  [{'ok ' if cond else 'FAIL'}] {name}" + ("" if cond else f"  {detail}"))
        if not cond:
            fails.append(name)

    root = Path(tempfile.mkdtemp(prefix="parktest-"))

    # -- a fresh install ------------------------------------------------------
    # The first command a new user runs is the one nobody on the project runs.
    d = root / "fresh"
    rc, out = park(d, "list")
    check("list on a store that does not exist yet is a clean empty state",
          rc == 0 and "nothing parked" in out, f"rc={rc} {out[:120]!r}")
    check("reading the lot does not create it", not (d / "lot").exists())

    # -- add ------------------------------------------------------------------
    d = root / "add"
    rc, it = park_json(d, "add", "--title", "Slack connector sync should retry on rate limits",
                       "--body", "The nightly sync dies on the first 429 and loses the batch.",
                       "--theme", "connectors", "--tier", "2", "--first-move", "wrap fetch in backoff",
                       "--owner-said", "--session", "s-add", "--source-kind", "handoff",
                       "--source-ref", "specs/_compaction/slack/v3.md", "--quote", "do this later",
                       "--linear", "PRODUCT-9", "--revisit-when", "a second 429 report")
    ok = rc == 0 and isinstance(it, dict)
    check("add writes an item and returns it", ok, f"rc={rc} {str(it)[:160]}")
    if ok:
        check("the id is kebab-case from the title", bool(ID_SHAPE.match(it["id"]))
              and it["id"].startswith("slack-connector-sync"), it["id"])
        check("every field lands where the contract says",
              it["theme"] == "connectors" and it["tier"] == 2 and it["status"] == "parked"
              and it["owner_said"] is True and it["first_move"] == "wrap fetch in backoff"
              and it["source"] == {"session_id": "s-add", "kind": "handoff",
                                   "ref": "specs/_compaction/slack/v3.md", "quote": "do this later"}
              and it["links"]["linear"] == "PRODUCT-9" and it["revisit_when"] == ["a second 429 report"],
              json.dumps(it)[:300])
        check("the add is the first history entry",
              len(it["history"]) == 1 and it["history"][0]["session_id"] == "s-add"
              and it["created"] == it["last_touched"], str(it.get("history")))
        f = d / "lot" / f"{it['id']}.json"
        check("one file per item, named by its id", f.exists() and json.loads(f.read_text())["id"] == it["id"])

    long_title = ("Rework the entire onboarding flow so that tablet users in split view can "
                  "finish the setup without rotating the device at all")
    rc, it = park_json(d, "add", "--title", long_title)
    check("a long title still gives an id of 60 characters at most",
          rc == 0 and len(it["id"]) <= 60 and ID_SHAPE.match(it["id"]), f"{rc} {it}")
    rc, it2 = park_json(d, "add", "--title", long_title, "--new")
    check("a second item with the same title gets its own id",
          rc == 0 and it2["id"] != it["id"] and len(it2["id"]) <= 60 and ID_SHAPE.match(it2["id"]),
          f"{rc} {it2}")

    rc, it = park_json(d, "add", "--title", "Body from stdin", "--body", "-",
                       stdin="read from a pipe\nwith two lines\n")
    check("--body - reads the body from stdin", rc == 0 and "read from a pipe" in it["body"], str(it)[:160])
    check("an item with no session given takes the session from the environment",
          rc == 0 and it["source"]["session_id"] == "sess-test", str(it)[:200])

    rc, it = park_json(d, "add", "--title", "A quote that runs long", "--quote", "x" * 500)
    check("a quote is kept to 200 characters", rc == 0 and len(it["source"]["quote"]) <= 200)

    rc, out = park(d, "add", "--title", "Unknown theme", "--theme", "vibes")
    check("an unknown theme is refused as bad input", rc == 2 and "theme" in out, f"rc={rc}")
    rc, out = park(d, "add", "--title", "   ")
    # Each refusal is checked by its words too: a missing script also exits 2.
    check("an empty title is refused", rc == 2 and "title" in out, f"rc={rc} {out[:120]}")
    rc, out = park(d, "add", "--title", "Bad tier", "--tier", "7")
    check("a tier outside 1-3 is refused", rc == 2 and "--tier" in out, f"rc={rc} {out[:120]}")
    rc, out = park(d, "add", "--title", "Bad id", "--id", "Not Kebab")
    check("an explicit id that is not kebab-case is refused", rc == 2 and "kebab" in out,
          f"rc={rc} {out[:120]}")

    # -- duplicates -----------------------------------------------------------
    # Ten sessions will notice the same problem. They should find one item.
    d = root / "dup"
    rc, first = park_json(d, "add", "--title", "Slack connector sync should retry on rate limits")
    rc, out = park(d, "add", "--title", "Slack sync needs retries when rate limited")
    check("a rephrasing of an open item is refused with a distinct code", rc == 3, f"rc={rc} {out[:120]}")
    check("the refusal names the item to update instead", first["id"] in out, out[:200])
    check("the refusal prints that item, so the caller can see it is the same work",
          "Slack connector sync should retry on rate limits" in out, out[:300])
    check("a refused duplicate writes nothing", len(list((d / "lot").glob("*.json"))) == 1)
    rc, out = park(d, "add", "--title", "Slack connector drops threads older than ninety days")
    check("a different item that shares a theme is not a duplicate", rc == 0, f"rc={rc} {out[:120]}")
    rc, out = park(d, "add", "--title", "Slack sync needs retries when rate limited", "--new")
    check("--new parks it anyway", rc == 0, f"rc={rc} {out[:120]}")

    # The threshold, from both sides: the share of the SHORTER title's content
    # words that the other title has. Stop words (a, the, to, on ...) do not count.
    d = root / "threshold"
    park(d, "add", "--title", "alpha bravo charlie delta echo")
    rc, _ = park(d, "add", "--title", "alpha bravo charlie delta")               # 4/4 of the shorter
    check("a title whose words are all in an open item is a duplicate", rc == 3, f"rc={rc}")
    rc, _ = park(d, "add", "--title", "alpha bravo charlie delta golf")          # 4/5 = 0.80
    check("overlap of exactly four fifths is a duplicate", rc == 3, f"rc={rc}")
    rc, _ = park(d, "add", "--title", "alpha bravo charlie golf hotel")          # 3/5 = 0.60
    check("overlap of three fifths is not", rc == 0, f"rc={rc}")
    rc, _ = park(d, "add", "--title", "The alpha bravo charlie delta echo for a")
    check("stop words are ignored in the comparison", rc == 3, f"rc={rc}")

    # The review's examples: distinct work on a shared subject must both park.
    d = root / "distinct"
    rc, _ = park(d, "add", "--title", "Add a retry to the queue consumer")
    rc2, out = park(d, "add", "--title", "Add a timeout to the queue consumer")
    check("a retry and a timeout on the same consumer are two items", rc == 0 and rc2 == 0,
          f"rc={rc},{rc2} {out[:160]}")
    rc, _ = park(d, "add", "--title", "Migrate the billing service")
    rc2, out = park(d, "add", "--title", "Migrate the search service")
    check("two migrations of different services are two items", rc == 0 and rc2 == 0,
          f"rc={rc},{rc2} {out[:160]}")
    rc, _ = park(d, "add", "--title", "Retry the Slack sync on rate limits")
    rc2, out = park(d, "add", "--title", "Slack sync: retry on rate limits")
    check("the same task in different words is still refused", rc == 0 and rc2 == 3,
          f"rc={rc},{rc2} {out[:160]}")
    rc, _ = park(d, "add", "--title", "Rotate the keys!")
    rc2, _ = park(d, "add", "--title", "rotate  the KEYS")
    check("titles with the same slug are the same item", rc == 0 and rc2 == 3, f"rc={rc},{rc2}")
    rc, out = park(d, "add", "--title", "Slack sync: retry on rate limits")
    check("the refusal names both ways out, same task first",
          0 < out.find("park.py set") < out.find("--new"), out[:400])
    body = ("The export job writes a partial CSV when the warehouse query times out, and the "
            "downstream import treats the partial file as complete and overwrites good rows.")
    park(d, "add", "--title", "Export job truncation", "--body", body)
    rc, _ = park(d, "add", "--title", "Warehouse timeout corrupts imports", "--body", body)
    check("a different title over the same body is a duplicate", rc == 3, f"rc={rc}")
    rc, done_it = park_json(d, "add", "--title", "hotel india juliet kilo")
    park(d, "done", done_it["id"])
    rc, _ = park(d, "add", "--title", "hotel india juliet kilo")
    check("a finished item does not block parking the same thing again", rc == 0, f"rc={rc}")

    # -- the tier 1 cap -------------------------------------------------------
    # The point of the feature. A sixth "do next" is a list nobody reads.
    d = root / "cap"
    names = ["router latency budget", "canvas zoom jitter", "deck export fonts",
             "eval harness flake", "memory compaction receipts"]
    ids = []
    for n in names:
        rc, it = park_json(d, "add", "--title", n, "--tier", "1")
        ids.append(it["id"] if rc == 0 else None)
    check("five tier 1 items fit", all(ids), str(ids))
    rc, out = park(d, "add", "--title", "security header audit", "--tier", "1")
    check("the sixth tier 1 item is refused with its own code", rc == 4, f"rc={rc} {out[:160]}")
    check("the refusal lists all five so the caller can choose",
          all(i and i in out for i in ids), out[:400])
    check("a refused sixth writes nothing", len(list((d / "lot").glob("*.json"))) == 5)
    rc, inbox = park_json(d, "add", "--title", "docs typo sweep")
    rc, out = park(d, "set", inbox["id"], "--tier", "1")
    check("promoting into a full tier 1 is refused too", rc == 4, f"rc={rc}")
    rc, out = park(d, "add", "--title", "security header audit", "--tier", "1", "--force")
    check("there is no --force", rc == 2 and "unrecognized arguments: --force" in out, f"rc={rc}")
    park(d, "set", ids[0], "--tier", "2")
    rc, out = park(d, "add", "--title", "security header audit", "--tier", "1")
    check("demoting one makes room", rc == 0, f"rc={rc} {out[:120]}")
    park(d, "done", ids[1])
    rc, out = park(d, "set", inbox["id"], "--tier", "1")
    check("a finished tier 1 item no longer holds a slot", rc == 0, f"rc={rc} {out[:120]}")
    rc, out = park(d, "set", ids[1], "--status", "parked")
    check("reopening a finished tier 1 item into a full tier is refused", rc == 4, f"rc={rc}")

    # -- list order and filters -----------------------------------------------
    d = root / "order"
    lot = d / "lot"
    write(lot, item("a-tier1-plain", tier=1, last_touched="2026-09-20T00:00:00Z"))
    write(lot, item("b-tier1-owner", tier=1, owner_said=True, last_touched="2026-09-01T00:00:00Z"))
    write(lot, item("c-tier2-newer", tier=2, last_touched="2026-09-28T00:00:00Z", theme="router"))
    write(lot, item("d-tier2-older", tier=2, last_touched="2026-09-10T00:00:00Z", theme="router"))
    write(lot, item("e-tier3", tier=3, status="in-progress"))
    write(lot, item("f-inbox-owner", tier=None, owner_said=True,
                    source={"session_id": "s-other", "kind": "session", "ref": None, "quote": None}))
    write(lot, item("g-done", tier=1, status="done"))
    write(lot, item("h-killed", tier=2, status="killed"))
    rc, rows = park_json(d, "list")
    got = [r["id"] for r in rows] if isinstance(rows, list) else rows
    want = ["b-tier1-owner", "a-tier1-plain", "c-tier2-newer", "d-tier2-older", "e-tier3", "f-inbox-owner"]
    check("list: tier 1, 2, 3, inbox; owner's asks first; newest touched first", got == want, f"got {got}")
    rc, out = park(d, "list")
    pos = [out.find(i) for i in want]
    check("the table prints in the same order", rc == 0 and all(p >= 0 for p in pos) and pos == sorted(pos),
          out[:400])
    age = (datetime.now(timezone.utc).date() - date(2026, 9, 1)).days
    b_row = next((l for l in out.splitlines() if "b-tier1-owner" in l), "")
    check("each row carries its age in days", f"{age}d" in b_row, b_row)
    check("an unsorted item reads as inbox, not as a tier",
          "inbox" in next((l for l in out.splitlines() if "f-inbox-owner" in l), ""))
    rc, rows = park_json(d, "list", "--tier", "1")
    check("--tier 1 lists only open tier 1", [r["id"] for r in rows] == ["b-tier1-owner", "a-tier1-plain"],
          str(rows)[:200])
    rc, rows = park_json(d, "list", "--tier", "inbox")
    check("--tier inbox lists only the unsorted", [r["id"] for r in rows] == ["f-inbox-owner"])
    rc, rows = park_json(d, "list", "--status", "done")
    check("--status reaches closed items", [r["id"] for r in rows] == ["g-done"])
    rc, rows = park_json(d, "list", "--owner-said")
    check("--owner-said lists only the owner's asks",
          [r["id"] for r in rows] == ["b-tier1-owner", "f-inbox-owner"])
    rc, rows = park_json(d, "list", "--theme", "router")
    check("--theme filters", [r["id"] for r in rows] == ["c-tier2-newer", "d-tier2-older"])

    # -- per session ----------------------------------------------------------
    d = root / "session"
    # Titles share no words, or the duplicate rule would (rightly) refuse them.
    rc, x = park_json(d, "add", "--title", "retry the slack sync", "--session", "s1")
    rc, y = park_json(d, "add", "--title", "canvas zoom jitter", "--session", "s2")
    rc, z = park_json(d, "add", "--title", "deck font fallback", "--session", "s3")
    rc, rows = park_json(d, "list", "--session", "s2")
    check("--session lists what that session parked", [r["id"] for r in rows] == [y["id"]], str(rows)[:200])
    park(d, "set", z["id"], "--tier", "3", "--session", "s1")
    rc, rows = park_json(d, "list", "--session", "s1")
    check("--session also lists what that session changed",
          sorted(r["id"] for r in rows) == sorted([x["id"], z["id"]]), str(rows)[:200])
    rc, rows = park_json(d, "list")
    check("with no filter, list is across every session", len(rows) == 3)

    # -- set / done / kill / show and the history they leave -------------------
    d = root / "history"
    rc, it = park_json(d, "add", "--title", "rewrite the router prompt", "--tier", "3")
    first_entry = it["history"][0]
    rc, it = park_json(d, "set", it["id"], "--tier", "2", "--theme", "router", "--linear", "PRODUCT-12",
                       "--pr", "https://github.com/o/r/pull/1", "--revisit-when", "eval drops")
    check("set applies every field it was given",
          rc == 0 and it["tier"] == 2 and it["theme"] == "router" and it["links"]["linear"] == "PRODUCT-12"
          and it["links"]["prs"] == ["https://github.com/o/r/pull/1"] and it["revisit_when"] == ["eval drops"],
          str(it)[:300])
    check("set appends to history and leaves earlier entries alone",
          len(it["history"]) == 2 and it["history"][0] == first_entry
          and "tier" in it["history"][1]["change"], str(it.get("history")))
    rc, same = park_json(d, "set", it["id"], "--tier", "2")
    check("a set that changes nothing adds no history", rc == 0 and len(same["history"]) == 2)
    rc, out = park(d, "set", it["id"], "--status", "killed")
    check("killing goes through kill, which asks why", rc == 2 and "kill" in out, f"rc={rc}")
    rc, out = park(d, "kill", it["id"])
    check("kill without --why is refused", rc == 2 and "--why" in out, f"rc={rc}")
    rc, it = park_json(d, "kill", it["id"], "--why", "the new router made it moot")
    check("kill records the reason in history",
          rc == 0 and it["status"] == "killed" and "moot" in it["history"][-1]["change"], str(it)[:300])
    rc, it = park_json(d, "add", "--title", "sweep stale feature flags")
    rc, it = park_json(d, "done", it["id"])
    check("done closes the item and says so in history",
          rc == 0 and it["status"] == "done" and it["history"][-1]["change"].startswith("done"))
    rc, out = park(d, "show", it["id"])
    check("show prints the item", rc == 0 and "sweep stale feature flags" in out, out[:200])
    rc, out = park(d, "show", "no-such-item")
    check("show on a missing id exits 1", rc == 1, f"rc={rc}")
    rc, out = park(d, "set", "no-such-item", "--tier", "1")
    check("set on a missing id exits 1", rc == 1, f"rc={rc}")

    # -- import ---------------------------------------------------------------
    # Backfilling from old handoffs is one pass over many lines, and one bad
    # line must not cost the rest of the file.
    d = root / "import"
    src = d / "items.jsonl"
    src.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"title": "Cache the connector token refresh", "theme": "connectors", "tier": 2},
        "{this is not json",
        {"title": "Canvas zoom snaps back on trackpad", "theme": "canvas",
         "revisit_when": ["a second report"]},
        {"title": "Connector token refresh: cache the results", "revisit_when": ["token errors in logs"]},
        {"title": "Unknown theme line", "theme": "vibes"},
        "",
        {"title": "Deck export drops custom fonts", "theme": "decks", "owner_said": True,
         "source": {"session_id": "s-old", "kind": "handoff", "ref": "v2.md", "quote": "later"}},
    ]
    src.write_text("\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines) + "\n")
    rc, res = park_json(d, "import", str(src))
    ok = isinstance(res, dict)
    check("import reports its counts", ok and (res.get("added"), res.get("merged"), res.get("rejected")) == (3, 1, 2),
          str(res)[:300])
    check("import exits 1 when a line was rejected, so a script notices", rc == 1, f"rc={rc}")
    probs = {p["line"] for p in res.get("problems", [])} if ok else set()
    check("each rejected line is named by its line number", probs == {2, 5}, str(probs))
    rc, rows = park_json(d, "list")
    by_title = {r["title"]: r for r in rows} if isinstance(rows, list) else {}
    cached = by_title.get("Cache the connector token refresh", {})
    check("the lines after a bad one were still imported", "Deck export drops custom fonts" in by_title,
          str(list(by_title)))
    check("a duplicate line is merged into the item it repeats",
          "token errors in logs" in cached.get("revisit_when", [])
          and any("merged" in h["change"] for h in cached.get("history", [])), str(cached)[:300])
    rc, out = park(d, "import", str(src))
    check("importing the same file twice adds nothing new", "0 added" in out, out[:200])

    # -- export ---------------------------------------------------------------
    d = root / "export"
    rc, a = park_json(d, "add", "--title", "unsorted thing to look at")
    rc, b = park_json(d, "add", "--title", "router fallback chain", "--tier", "2", "--theme", "router",
                      "--linear", "PRODUCT-7", "--revisit-when", "latency over 5s", "--body", "Why it matters.")
    rc, c = park_json(d, "add", "--title", "finished and gone")
    park(d, "done", c["id"])
    out_dir = d / "export-out"
    rc, out = park(d, "export", "--to", str(out_dir))
    import yaml
    files = sorted(p.name for p in out_dir.glob("*.md")) if out_dir.exists() else []
    check("export writes one markdown file per open item",
          rc == 0 and files == sorted([f"{a['id']}.md", f"{b['id']}.md"]), f"rc={rc} {files}")

    def front(p: Path) -> tuple[dict, str]:
        text = p.read_text()
        _, fm, rest = text.split("---\n", 2)
        return yaml.safe_load(fm), rest

    if files:
        fa, _ = front(out_dir / f"{a['id']}.md")
        fb, rest_b = front(out_dir / f"{b['id']}.md")
        check("front matter carries exactly the agreed keys, in order",
              list(fb) == ["id", "title", "created", "last_touched", "priority_tier", "status",
                           "theme", "linear_ticket", "revisit_triggers"], str(list(fb)))
        check("an inbox item exports priority_tier: null", fa["priority_tier"] is None, str(fa))
        check("a sorted item exports its tier, ticket and triggers",
              fb["priority_tier"] == 2 and fb["linear_ticket"] == "PRODUCT-7"
              and fb["revisit_triggers"] == ["latency over 5s"] and fb["status"] == "parked", str(fb))
        check("the body follows the front matter", "Why it matters." in rest_b)

    # -- check ----------------------------------------------------------------
    d = root / "check"
    rc, k1 = park_json(d, "add", "--title", "retry the slack sync")
    rc, k2 = park_json(d, "add", "--title", "canvas zoom snaps back")
    park(d, "done", k2["id"])

    def handoff(text: str) -> Path:
        p = d / f"handoff-{abs(hash(text))}.md"
        p.write_text(text)
        return p

    good = handoff(f"""# Handoff v3

## Done
- shipped the thing

## Parked
- lot:{k1['id']} retry the slack sync on 429
- canvas zoom, finished since: lot:{k2['id']}
  - a nested note under it needs no ref of its own

## Next
- this line is outside the section and needs no ref
""")
    rc, out = park(d, "check", str(good))
    check("check passes when every parked line names an item in the lot", rc == 0, f"rc={rc} {out[:200]}")

    bad = handoff(f"""## Parked work
- lot:{k1['id']} fine
- retry the export job someday
- lot:not-a-real-item looks referenced but is not
""")
    rc, out = park(d, "check", str(bad))
    check("check fails on a parked line with no lot reference", rc == 1 and "retry the export job" in out,
          f"rc={rc} {out[:300]}")
    check("check names a reference to an item that does not exist", "not-a-real-item" in out, out[:300])
    check("check gives the offending line numbers", "line 3" in out and "line 4" in out, out[:300])

    table = handoff(f"""### Deferred

| item | why |
|------|-----|
| lot:{k1['id']} | later |
| export job retry | forgot to park it |
""")
    rc, out = park(d, "check", str(table))
    check("a Deferred table is checked row by row, header skipped",
          rc == 1 and "export job retry" in out and "| item |" not in out, f"rc={rc} {out[:300]}")

    # Headings the real handoffs use for left-over work, one check each. A
    # bare bullet under each has no lot ref, so check must fail on it.
    for head in ("Parking lot", "Backlog", "Follow-ups", "Follow ups", "Later", "Not done",
                 "Open items", "Next session", "3. Follow-ups", "### Backlog (parking-lot candidates)"):
        text = head if head.startswith("#") else f"## {head}"
        h = handoff(f"# Handoff\n\n{text}\n- export job retry\n")
        rc, out = park(d, "check", str(h))
        check(f"check reads a '{head}' heading", rc == 1 and "export job retry" in out, f"rc={rc} {out[:200]}")
    prose = handoff("# Handoff\n\n## Decisions\n- export job retry\n\n"
                    "Parking is expensive, so we kept the lot small.\n- a bullet after prose\n")
    rc, out = park(d, "check", str(prose))
    check("a Decisions heading and a prose line that starts with Parking are not headings",
          rc == 0, f"rc={rc} {out[:200]}")
    rc, out = park(d, "check", str(handoff("## Latest news\n- export job retry\n")))
    check("Latest is not Later", rc == 0, f"rc={rc} {out[:200]}")

    none = handoff("""# Handoff\n\n## Done\n- a thing\n\n```\n## Parked\n- inside a code block\n```\n""")
    rc, out = park(d, "check", str(none))
    check("a handoff with no Parked or Deferred section passes", rc == 0, f"rc={rc} {out[:200]}")
    rc, out = park(d, "check", str(d / "missing.md"))
    check("a missing handoff file is bad input", rc == 2 and "no such file" in out, f"rc={rc}")

    # -- atomic writes --------------------------------------------------------
    stray = [p.name for sub in root.iterdir() for p in (sub / "lot").glob("*")
             if (sub / "lot").exists() and not p.name.endswith(".json") and p.name != ".lock"]
    check("no write anywhere in this suite left a temp file behind", not stray, str(stray))

    # A write that fails half way must leave the old file whole.
    d = root / "atomic"
    rc, it = park_json(d, "add", "--title", "a file that must survive a failed write")
    target = d / "lot" / f"{it['id']}.json"
    before = target.read_bytes()
    os.environ["RECALL_LOT_DIR"] = str(d / "lot")
    os.environ["RECALL_HOME"] = str(d / "recall-home")
    sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location("park_under_test", PARK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    real_replace = os.replace

    def boom(*a, **k):
        raise OSError("disk full")

    os.replace = boom
    try:
        rc = mod.main(["set", it["id"], "--tier", "3"])
    except Exception:
        rc = "raised"
    finally:
        os.replace = real_replace
    check("a failed write is reported, not swallowed", rc not in (0, "raised"), f"rc={rc}")
    check("a failed write leaves the old file byte for byte", target.read_bytes() == before)
    left = [p.name for p in (d / "lot").iterdir() if p.name not in (target.name, ".lock")]
    check("a failed write leaves no temp file", not left, str(left))

    # -- the real store is never the default in a test -------------------------
    d = root / "paths"
    rc, _ = park(d, "add", "--title", "goes where RECALL_LOT_DIR says")
    check("RECALL_LOT_DIR wins over RECALL_HOME",
          rc == 0 and len(list((d / "lot").glob("*.json"))) == 1
          and not (d / "recall-home" / "lot").exists())
    env = env_for(d)
    env.pop("RECALL_LOT_DIR")
    env["OKWOW_LOT_DIR"] = str(d / "okwow-lot")
    rc, _ = park(d, "add", "--title", "goes where OKWOW_LOT_DIR says", env=env)
    check("OKWOW_LOT_DIR is honoured like the other OKWOW_ paths",
          rc == 0 and len(list((d / "okwow-lot").glob("*.json"))) == 1)
    env.pop("OKWOW_LOT_DIR")
    rc, _ = park(d, "add", "--title", "goes under RECALL_HOME by default", env=env)
    check("with no lot override, the store follows RECALL_HOME and nothing else",
          rc == 0 and len(list((d / "recall-home" / "lot" / "items").glob("*.json"))) == 1
          and not (d / "home").exists())

    print(f"\n{'FAIL' if fails else 'PASS'} {ran[0] - len(fails)}/{ran[0]} parking-lot checks")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
