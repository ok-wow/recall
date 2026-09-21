#!/usr/bin/env python3
"""Build the probe index the injector matches against.

The catalogs are 2.2 MB of YAML. Parsing them on every user prompt is not
affordable, so this precomputes a token -> entry map and the injector loads
only that. Rebuilt when a catalog's mtime moves.

Only BACKTICKED probe_when items are indexed. That convention is already
documented in okwow-compound/SKILL.md ("Make probe_when mechanically
matchable") and it is the only part of a probe item that is safe to match on
literally -- plain prose items are semantic and would fire on anything.

Two things this build owes its operator, because nobody watches it run:
  - it REPORTS what it dropped and why (see build()). Printing only a success
    count is how a 43% coverage gap stayed invisible for months.
  - it REFUSES to publish a partial index (see load_catalog()). A catalog that
    vanished or stopped parsing is a broken build, not a smaller corpus.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

import yaml

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
    return default


# Overridable for exactly the reason recall.py's are: the suite has to build an
# index out of synthetic catalogs, and a test that must overwrite the
# production index in order to assert anything is not a test.
RECALL_HOME = env_path("RECALL_HOME", "OKWOW_HOME", Path.home() / ".recall")
CATALOG_DIR = env_path("RECALL_CATALOG_DIR", "OKWOW_CATALOG_DIR", RECALL_HOME / "catalogs")
CATALOGS = {
    "FM": CATALOG_DIR / "FAILURE_MODES.yaml",
    "PF": CATALOG_DIR / "PROCESS_FAILURES.yaml",
}
INDEX_PATH = env_path("RECALL_PROBE_INDEX", "OKWOW_PROBE_INDEX",
                      RECALL_HOME / "probe-index.json")
# A refused build leaves the last good index in place, which is right, but it
# also makes the failure invisible: okwow-compound-sessionstart.sh rebuilds in
# the background as `(python3 "$PROBE_BUILDER" >/dev/null 2>&1 &)` -- stdout,
# stderr and the exit code all discarded. Worse, the frozen index keeps failing
# the hook's mtime staleness test, so it silently retries every session
# forever. So the reason is left on disk, next to the index, for a health
# surface to find.
ERROR_MARKER = INDEX_PATH.with_name(INDEX_PATH.name + ".error")

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

# Environment furniture. These are real code tokens, and idf over a 677-entry
# catalog rates them MAXIMALLY specific because each appears in exactly one
# entry -- while in fact they appear in nearly every terminal, path and task
# notification in existence. Catalog rarity is not world rarity, and nothing
# computed from this corpus can tell the difference, so they are named here.
# `/tmp/` alone fired a PR-hygiene entry against a background-task notification.
PATH_FURNITURE = {"specs/", "node_modules/", ".git/"}

PATH_CHARS = re.compile(r"^[~.]?[A-Za-z0-9._~/-]+$")


def is_path_furniture(token: str) -> bool:
    """A bare absolute/home-relative DIRECTORY path carries no failure signal.

    Listing the offenders literally was whack-a-mole: `/tmp/` was caught and
    `~/dev/specs` (no trailing slash) walked straight through. This is the
    shape instead. Two deliberate exemptions:
      - a path naming a concrete FILE (`~/.claude/settings.json`) still means
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


# File FORMAT suffixes, as a literal vocabulary. A shape rule was tried first
# and rejected by measurement: `^\.[a-z0-9]+$` also matches CSS selectors
# (`.card`, `.chip`, `.eyebrow`), dotfiles and dot-directories (`.gitignore`,
# `.venv`, `.nojekyll`) and attribute access (`.length`) -- it dropped 28
# tokens, most of them genuinely diagnostic. Nothing about the shape of
# `.json` distinguishes it from `.card`; the difference is that one names a
# FORMAT the whole world writes about and the other names a thing in this
# codebase. Unlike a list of offenders, this one does not grow as the corpus
# grows: the set of file formats is closed.
BARE_FILE_EXTENSIONS = frozenset({
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".html", ".xhtml", ".xml",
    ".md", ".mdx", ".csv", ".tsv", ".txt", ".log", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".heic", ".svg", ".pdf", ".zip", ".scss", ".less",
    ".plist", ".ipynb", ".sqlite", ".lock", ".patch", ".diff", ".conf",
    ".ini", ".cfg",
})


def is_bare_extension(token: str) -> bool:
    """A file EXTENSION on its own names a FORMAT, not a situation.

    PATH_FURNITURE's trap one level down, and it needs its own rule because the
    exemption above is what lets it through: an extension is how
    is_path_furniture recognises a real file. Measured 2026-09-13: `.json` and
    `.yaml` are both indexed against the SAME entry, so "update the .json
    config and the .yaml settings for the deploy" scored 13.33 -- two specific
    tokens, past MIN_SCORE and past VISIBLE_MIN_SCORE, so an ordinary prompt
    about two config files injected an entry AND interrupted the user with a
    "we hit this before" notice. A named FILE (`package.json`) still
    identifies something; the extension alone does not.
    """
    return token in BARE_FILE_EXTENSIONS


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


# A backticked MULTI-WORD phrase beginning with a CLI name is a command, and a
# command is as distinctive as any punctuation-bearing token. The shape test
# missed this entire class: `git status` has no punctuation, no internal capital,
# no digit and is under the length floor, so it scored as ordinary English and
# could never trigger a hit on its own. Measured 2026-09-14: 48 of 785 indexed
# entries could never fire because every token they had was classified weak --
# counted as covered, and not.
#
# The list deliberately EXCLUDES CLI names that are also ordinary English --
# go, open, find, make, node, tar, swift -- because `go look`, `node graph` and
# `find it` appear in normal prose and would fire on nothing. A phrase is
# promoted only when its first word is a tool name a person does not use as a
# verb in conversation.
COMMAND_WORDS = frozenset("""
git gh npm npx pnpm yarn docker alembic pytest cargo python python3 brew
launchctl kubectl terraform gcloud aws ssh curl systemctl psql redis-cli
vitest jest eslint tsc ruff mypy poetry pip uv rustc xcodebuild killall
pkill crontab sed awk chmod chown rg jq
""".split())


def is_command_phrase(token: str) -> bool:
    parts = token.split()
    return len(parts) >= 2 and parts[0].lower() in COMMAND_WORDS


def is_specific(token: str) -> bool:
    if is_command_phrase(token):
        return True
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
    if is_bare_extension(token):
        return False
    # Pure prose fragments ("before any commit in a shared checkout") are
    # semantic, not literal; they belong to the model's judgment, not a grep.
    if token.count(" ") > 3:
        return False
    return True


# The corpus has 151 distinct field names and `id` is the only one present in
# every entry, so a fixed allow-list cannot reliably find the body. The four
# names below found nothing in 197 of 783 indexed entries (25%), and those
# rendered into live sessions as an id and a matched-token list with NO "what
# happened" line -- the reader gets told a lesson applies and not what it is.
# Same class of bug recall.py was just fixed for. The original four stay first
# and in their original order (they are the best-written fields); everything
# after them is fallback, ordered by how much of an account of the failure the
# field actually tends to hold.
SUMMARY_FIELDS = (
    "summary", "trigger", "what_failed", "context",
    "what", "what_happens", "what_went_wrong", "failure", "verified_failure",
    "symptom", "pattern", "lesson", "description", "root_cause",
    "why_it_matters", "one_line", "title", "consequence",
)
REMEDY_FIELDS = (
    "fix_pattern", "fix", "affected_pattern",
    "remedy", "correct_behavior", "rule", "prevention", "validation",
    "verified_mechanism", "doctrine", "workaround",
)

# Short slugs, provenance, dates and the probe machinery. The last-resort
# longest-field fallback must not reach these: `failure_class: visual-slop` is
# a label, not an account, and rendering it as "what happened" is worse than
# rendering nothing.
NON_BODY_FIELDS = frozenset({
    "id", "probe_when", "probe_type", "probe_implementation", "failure_class",
    "category", "repo", "scope", "surface", "area", "type", "severity",
    "status", "confidence", "synchronization", "source_session", "caught_in",
    "artifact", "artifact_target", "related", "version", "recurrences",
    "date", "logged", "discovered", "compounded_at", "date_observed",
    "date_learned", "verification_date", "verified_at", "diagnostic_question",
})
# `affected_pattern` sits in REMEDY_FIELDS only as a third choice -- it
# describes the SITUATION, not the cure -- and for 8 indexed entries it is the
# only account of what happened that exists. So it is the one remedy candidate
# the summary may still use, and only when the "what to do" line came from
# somewhere else: build() passes that field name in, so the two lines can never
# be the same paragraph.
SUMMARY_BLOCK = frozenset(REMEDY_FIELDS) - {"affected_pattern"}

# Below this a "body" is a tag, not a sentence.
MIN_FALLBACK_LEN = 40
REMEDY_NAME_HINTS = ("fix", "remed", "prevent", "instead", "correct", "mitigat", "guard")


# What an injected entry is allowed to carry. A CEILING, not a spend: an entry
# shorter than this costs its own length, so raising it only bills the long tail.
#
# It was 400 for a long time, as a bare literal with no comment and no
# measurement. Measured 2026-09-21 over 120 entries nobody had rewritten, asking
# a judge whether the clipped version still let the reader act:
#
#     cap     still arriving with a piece missing
#      400    93/120   77.5%
#      800    12/120   10.0%
#     1200     3/120    2.5%
#
# The price, over 2,719 displayed fields in the live corpus: mean delivered per
# field 335 -> 380 characters. Uncapped is 383, because only 1% of fields exceed
# 1200. The injector shows at most MAX_ENTRIES=2 entries of two fields, so this
# costs about 45 tokens on a turn that fires at all.
PUSH_CAP = int(os.environ.get("RECALL_PUSH_CAP")
                or os.environ.get("OKWOW_PUSH_CAP") or 1200)


def _clean(value: str) -> str:
    return " ".join(str(value).split())[:PUSH_CAP]


def _looks_like_remedy(name: str) -> bool:
    return any(hint in name for hint in REMEDY_NAME_HINTS)


def _pick(entry: dict, names: tuple[str, ...], exclude: frozenset[str]) -> tuple[str, str]:
    for field in names:
        if field in exclude:
            continue
        v = entry.get(field)
        if isinstance(v, str) and v.strip():
            return field, _clean(v)
    return "", ""


def _longest(entry: dict, blocked: frozenset[str], want_remedy: bool) -> tuple[str, str]:
    """Whatever this entry decided to call its body (or its cure).

    Longest wins because the body IS the longest thing an entry carries, and
    NON_BODY_FIELDS keeps the short slugs out of the running.
    """
    best_name, best = "", ""
    for name, v in entry.items():
        if name in NON_BODY_FIELDS or name in blocked:
            continue
        if _looks_like_remedy(name) != want_remedy:
            continue
        if isinstance(v, str) and len(v.strip()) >= MIN_FALLBACK_LEN and len(v) > len(best):
            best_name, best = name, v
    return (best_name, _clean(best)) if best else ("", "")


def remedy_field(entry: dict) -> tuple[str, str]:
    """The "what to do" line, and which field it came from.

    build() needs the field NAME so the summary can be told what is already
    spoken for -- a hit that prints the same paragraph on both lines is worse
    than one that prints it once.
    """
    name, text = _pick(entry, REMEDY_FIELDS, frozenset())
    return (name, text) if text else _longest(entry, frozenset(), want_remedy=True)


def remedy(entry: dict) -> str:
    return remedy_field(entry)[1]


def summarize(entry: dict, exclude: str = "") -> str:
    blocked = SUMMARY_BLOCK | ({exclude} if exclude else frozenset())
    _name, text = _pick(entry, SUMMARY_FIELDS, blocked)
    return text or _longest(entry, blocked, want_remedy=False)[1]


class CatalogError(RuntimeError):
    """A catalog could not be read, so this build must not publish anything."""


def load_catalog(label: str, path: Path) -> list:
    """Read one catalog, or refuse the whole build.

    Both halves of this were live bugs, measured 2026-09-13:
      - a missing catalog was `continue`d, and the build then printed
        "indexed 1 entries" as SUCCESS over an index that had silently lost
        every entry of the absent file;
      - an unparseable catalog raised out of build(), and because the
        sessionstart rebuild discards stdout, stderr and the exit code, the
        index simply froze at whatever it last was, forever, with no alarm.
    Half a corpus that looks whole is the worse of the two, so neither is
    tolerated: raise, and let main() keep the last good index and say why.
    """
    if not path.exists():
        raise CatalogError(f"{label}: {path} is missing")
    try:
        data = yaml.safe_load(path.read_text())
    except Exception as exc:                      # YAMLError, UnicodeDecodeError, OSError
        first = " ".join(str(exc).split())[:200]
        raise CatalogError(f"{label}: {path} did not parse: {first}") from exc
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                data = v
                break
    if data is None:
        return []                                 # a genuinely empty catalog parses fine
    if not isinstance(data, list):
        raise CatalogError(
            f"{label}: {path} holds {type(data).__name__}, expected a list of entries")
    return data


def build() -> dict:
    entries: dict[str, dict] = {}
    postings: dict[str, list[str]] = {}
    specific_tokens: set[str] = set()

    # Why an entry never reaches the injector. Counting this is the entire
    # point: 426 entries HAVE probe_when and still produce no token, and the
    # only reason that went unnoticed for months is that the build printed a
    # success count and nothing else.
    dropped: dict[str, int] = {
        "no_id": 0,
        "no_probe_when": 0,
        "no_backticked_token": 0,
        "all_tokens_filtered": 0,
        "not_a_mapping": 0,
    }
    examples: dict[str, list[str]] = {k: [] for k in dropped}
    seen = 0
    empty_catalogs: list[str] = []

    def drop(reason: str, eid: object) -> None:
        dropped[reason] += 1
        if len(examples[reason]) < 5 and isinstance(eid, str):
            examples[reason].append(eid)

    for label, path in CATALOGS.items():
        data = load_catalog(label, path)
        if not data:
            empty_catalogs.append(label)
        for entry in data:
            seen += 1
            if not isinstance(entry, dict):
                drop("not_a_mapping", None)
                continue
            eid = entry.get("id")
            pw = entry.get("probe_when")
            if not eid:
                drop("no_id", None)
                continue
            if not pw:
                drop("no_probe_when", eid)
                continue
            items = pw if isinstance(pw, list) else [pw]
            tokens = set()
            specific_here = set()
            saw_backtick = False
            for item in items:
                if not isinstance(item, str):
                    continue
                for raw in BACKTICK.findall(item):
                    saw_backtick = True
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
                drop("all_tokens_filtered" if saw_backtick else "no_backticked_token", eid)
                continue
            key = f"{label}:{eid}"
            fix_field, fix_text = remedy_field(entry)
            entries[key] = {
                "id": eid,
                "catalog": label,
                "recurrences": entry.get("recurrences", 0),
                "summary": summarize(entry, exclude=fix_field),
                "fix": fix_text,
            }
            for t in tokens:
                postings.setdefault(t, []).append(key)
            specific_tokens.update(specific_here)

    # Inverse document frequency: a token in 40 entries identifies none of
    # them. Weighting beats a hard cutoff -- it lets a broad token still break
    # a tie between two entries that both matched something specific.
    total = max(len(entries), 1)
    weights = {t: round(math.log(total / len(e)), 4) for t, e in postings.items()}

    diagnostics = {
        "entries_seen": seen,
        "entries_indexed": len(entries),
        "dropped_total": sum(dropped.values()),
        "dropped": dropped,
        "dropped_examples": examples,
        # A hit that renders without these lines tells the reader an entry
        # applies and not what it says, so the build measures its own output.
        "entries_without_summary": sum(1 for e in entries.values() if not e["summary"]),
        "entries_without_fix": sum(1 for e in entries.values() if not e["fix"]),
        "empty_catalogs": empty_catalogs,
    }

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
        "diagnostics": diagnostics,
    }


def write_index(index: dict) -> None:
    """Unique temp file, fsync, atomic rename.

    The old code wrote a FIXED `<index>.json.tmp`. Two rebuilds can race --
    SessionStart fires one in the background on every new session, so two
    sessions opening together collide, as does a manual foreground rebuild
    racing a backgrounded one -- and interleaved writes into a single shared
    temp file get published by os.replace as whatever mixture was on disk.
    (An earlier version of this comment also claimed the drain rebuilds after
    each distillation. It does not: SessionStart is the only caller in the
    tree. The race above is real on its own, but the comment is the record of
    WHY this guard exists, so it may not carry a mechanism that does not.) A corrupt index is not a degraded index: the injector's load_index()
    swallows the JSONDecodeError and returns None, so retrieval stops
    completely, permanently and silently.
    """
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(INDEX_PATH.parent), prefix=INDEX_PATH.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(index, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())          # rename must not publish an empty file
        os.chmod(tmp, 0o644)               # mkstemp is 0600; the index is not a secret
        os.replace(tmp, INDEX_PATH)        # same directory, so this is atomic
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_error_marker(reason: str | None) -> None:
    try:
        if reason is None:
            ERROR_MARKER.unlink(missing_ok=True)
        else:
            ERROR_MARKER.write_text(reason.rstrip() + "\n")
    except OSError:
        pass                               # a diagnostic must never break the build


def report(index: dict) -> str:
    d = index["diagnostics"]
    size = INDEX_PATH.stat().st_size / 1024 if INDEX_PATH.exists() else 0
    lines = [
        f"indexed {d['entries_indexed']} entries, "
        f"{len(index['postings'])} tokens -> {INDEX_PATH} ({size:.0f} KB)"
    ]
    if d["dropped_total"]:
        pct = 100 * d["dropped_total"] / max(d["entries_seen"], 1)
        lines.append(
            f"dropped {d['dropped_total']} of {d['entries_seen']} entries ({pct:.1f}%) "
            f"— auto-injection cannot see them:")
        for reason, label in (
            ("no_backticked_token", "probe_when has no backticked token"),
            ("all_tokens_filtered", "every backticked token was filtered (stopword/short/furniture)"),
            ("no_probe_when", "no probe_when field"),
            ("no_id", "no id"),
            ("not_a_mapping", "row is not a mapping"),
        ):
            n = d["dropped"][reason]
            if n:
                eg = d["dropped_examples"][reason]
                lines.append(f"  {n:5}  {label}" + (f"   e.g. {eg[0]}" if eg else ""))
        lines.append("  recall.py reaches all of them; only auto-injection is limited.")
    if d["entries_without_summary"]:
        lines.append(
            f"WARNING: {d['entries_without_summary']} indexed entries have no summary — "
            "they would render with no 'what happened' line.")
    if d["empty_catalogs"]:
        lines.append(f"WARNING: empty catalog(s): {', '.join(d['empty_catalogs'])}")
    return "\n".join(lines)


def main() -> int:
    try:
        index = build()
    except CatalogError as exc:
        reason = (
            f"build_probe_index: REFUSED to write {INDEX_PATH} — {exc}. "
            "The previous index is left in place; retrieval keeps running on it "
            "but will not see anything added since. Fix the catalog and rerun.")
        set_error_marker(reason)
        sys.stderr.write(reason + "\n")
        return 2
    write_index(index)
    set_error_marker(None)
    print(report(index))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
