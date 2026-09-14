#!/usr/bin/env python3
"""Build the probe index the injector matches against.

The catalogs are megabytes of YAML. Parsing them on every user prompt is not
affordable, so this precomputes a token -> entry map and the injector loads
only that. Rebuilt when a catalog's mtime moves.

Only BACKTICKED probe_when items are indexed. That convention is documented
with the catalog format ("Make probe_when mechanically matchable") and it is
the only part of a probe item that is safe to match on literally -- plain
prose items are semantic and would fire on anything.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path

import yaml


def env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


RECALL_HOME = env_path("RECALL_HOME", Path.home() / ".recall")
CATALOG_DIR = env_path("RECALL_CATALOG_DIR", RECALL_HOME / "catalogs")

CATALOGS = {
    "FM": CATALOG_DIR / "FAILURE_MODES.yaml",
    "PF": CATALOG_DIR / "PROCESS_FAILURES.yaml",
    "DE": CATALOG_DIR / "DECISIONS.yaml",
}
# Honour the override the suite has been setting all along. Until 2026-09-14
# nothing read RECALL_PROBE_INDEX, so test_recall believed it had redirected the
# index and was in fact reading the user's real one.
INDEX_PATH = env_path("RECALL_PROBE_INDEX", RECALL_HOME / "probe-index.json")

BACKTICK = re.compile(r"`([^`]+)`")

# Tokens that match constantly and mean nothing on their own. A probe that
# fires on the word "main" fires on every session and teaches the reader to
# ignore the channel, which costs more than the entry was worth.
STOPTOKENS = {
    "main", "transform", "verified", "error", "true", "false", "null", "test",
    "tests", "build", "run", "git", "npm", "python", "node", "yes", "no",
    "todo", "fix", "fixme", "id", "url", "src", "dist", "app", "api", "data",
    "type", "types", "value", "name", "key", "class", "style", "color",
}

# Environment furniture. These are real code tokens, and idf over a few-hundred
# entry catalog rates them MAXIMALLY specific because each appears in exactly
# one entry -- while in fact they appear in nearly every terminal, path and task
# notification in existence. Catalog rarity is not world rarity, and nothing
# computed from this corpus can tell the difference, so they are named here.
# `/tmp/` alone fired a PR-hygiene entry against a background-task notification.
PATH_FURNITURE = {"specs/", "node_modules/", ".git/"}

PATH_CHARS = re.compile(r"^[~.]?[A-Za-z0-9._~/-]+$")


def is_path_furniture(token: str) -> bool:
    """A bare absolute/home-relative DIRECTORY path carries no failure signal.

    Listing the offenders literally was whack-a-mole: `/tmp/` was caught and
    `~/projects/specs` (no trailing slash) walked straight through. This is the
    shape instead. Two deliberate exemptions:
      - a path naming a concrete FILE (`~/.config/settings.json`) still means
        something, so an extension on the last segment keeps it;
      - it must be anchored at `/`, `~`, `./` or `../`, which leaves relative
        tokens like `origin/main` (a git ref, genuinely diagnostic) and
        `artifacts/html` alone.
    """
    if "/" not in token or not token.startswith(("~", "/", "./", "../")):
        return False
    if not PATH_CHARS.match(token):
        return False
    last = token.rstrip("/").rsplit("/", 1)[-1]
    if "." in last and not last.startswith("."):
        return False
    return True
MIN_TOKEN_LEN = 5

# A match needs at least one SPECIFIC token. Length was the first proxy for
# that and it was wrong: the first live fire matched `status`, `completed` and
# `background` — 6-10 characters of ordinary English — against a system
# notification. Specificity is about SHAPE, not size. A backticked probe token
# is meant to be code, and code is recognisable: it carries punctuation
# (`dark:bg-`, `origin/main`, `table=True`, `.env`), or internal capitals
# (`getComputedStyle`), or it is long enough that an English collision is
# unlikely. A plain lowercase word shorter than that can add to a score but can
# never trigger a hit by itself.
LONE_WORD_SPECIFIC_LEN = 14
CODE_PUNCT = set("._-/\\:=()[]{}<>*@#$%&+|!?,;\"'`~^")


def is_specific(token: str) -> bool:
    if any(c in CODE_PUNCT for c in token):
        return True
    # INTERNAL capitals only. A leading capital is just a UI label -- `Refresh`,
    # `Notify` and `Follow` are catalogued button names, and treating them as
    # code let "refresh the widget and notify me" score 19.5. camelCase means
    # a capital that is not the first character: getComputedStyle, ApexCharts.
    if any(c.isupper() for c in token[1:]):
        return True
    if any(c.isdigit() for c in token):
        return True
    return len(token) >= LONE_WORD_SPECIFIC_LEN


def normalize(token: str) -> str:
    return " ".join(token.strip().split()).lower()


def usable(token: str) -> bool:
    if len(token) < MIN_TOKEN_LEN:
        return False
    if token in STOPTOKENS or token in PATH_FURNITURE:
        return False
    if is_path_furniture(token):
        return False
    # Pure prose fragments ("before any commit in a shared checkout") are
    # semantic, not literal; they belong to the model's judgment, not a grep.
    if token.count(" ") > 3:
        return False
    return True


def summarize(entry: dict) -> str:
    for field in ("summary", "trigger", "what_failed", "context", "decided"):
        v = entry.get(field)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:400]
    return ""


def remedy(entry: dict) -> str:
    for field in ("fix_pattern", "fix", "affected_pattern", "why"):
        v = entry.get(field)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:400]
    return ""


def build() -> dict:
    entries: dict[str, dict] = {}
    postings: dict[str, list[str]] = {}
    specific_tokens: set[str] = set()

    for label, path in CATALOGS.items():
        if not path.exists():
            continue
        data = yaml.safe_load(path.read_text())
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    data = v
                    break
        for entry in data or []:
            if not isinstance(entry, dict):
                continue
            eid = entry.get("id")
            pw = entry.get("probe_when")
            if not eid or not pw:
                continue
            items = pw if isinstance(pw, list) else [pw]
            tokens = set()
            specific_here = set()
            for item in items:
                if not isinstance(item, str):
                    continue
                for raw in BACKTICK.findall(item):
                    raw = " ".join(raw.strip().split())
                    tok = raw.lower()
                    if not usable(tok):
                        continue
                    tokens.add(tok)
                    # judged on the ORIGINAL casing — lowercasing first would
                    # erase exactly the camelCase signal this depends on
                    if is_specific(raw):
                        specific_here.add(tok)
            if not tokens:
                continue
            key = f"{label}:{eid}"
            entries[key] = {
                "id": eid,
                "catalog": label,
                "recurrences": entry.get("recurrences", 0),
                "summary": summarize(entry),
                "fix": remedy(entry),
            }
            for t in tokens:
                postings.setdefault(t, []).append(key)
            specific_tokens.update(specific_here)

    # Inverse document frequency: a token in 40 entries identifies none of
    # them. Weighting beats a hard cutoff -- it lets a broad token still break
    # a tie between two entries that both matched something specific.
    total = max(len(entries), 1)
    weights = {t: round(math.log(total / len(e)), 4) for t, e in postings.items()}

    return {
        "version": 1,
        "built_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        "sources": {
            label: {"path": str(p), "mtime": int(p.stat().st_mtime) if p.exists() else 0}
            for label, p in CATALOGS.items()
        },
        "specific_tokens": sorted(specific_tokens),
        "entries": entries,
        "postings": postings,
        "weights": weights,
    }


def main() -> int:
    index = build()
    # The state root is configurable and may not exist yet on a first run.
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, separators=(",", ":")))
    os.replace(tmp, INDEX_PATH)
    print(
        f"indexed {len(index['entries'])} entries, "
        f"{len(index['postings'])} tokens -> {INDEX_PATH} "
        f"({INDEX_PATH.stat().st_size / 1024:.0f} KB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
