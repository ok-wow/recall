#!/usr/bin/env python3
"""Agent memory notes, as recall sees them -- recall.py load_memory_notes.

A fact saved as a note should be found before it is re-derived, whichever of
the three routes leads to its folder. Every case runs against fixtures under a
temporary HOME, so nothing here can read a real memory folder.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RECALL = REPO / "scripts" / "recall.py"
FIXTURES = REPO / "tests" / "fixtures" / "memory"

FAILS: list[str] = []
TOTAL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    if ok:
        print(f"  [ok ] {name}")
    else:
        FAILS.append(name)
        print(f"  [FAIL] {name}  {detail}"[:600])


def env_for(d: Path, memory_dirs: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("RECALL_", "OKWOW_"))}
    # Every other source points at nothing, so the counts below are the notes'.
    env.update({"HOME": str(d / "home"),
                "RECALL_HOME": str(d / "recall-home"),
                "RECALL_MEMORY_DIRS": memory_dirs,
                "RECALL_CATALOG_DIR": str(d / "catalogs"),
                "RECALL_LOT_DIR": str(d / "no-lot"),
                "RECALL_SPECS_DIR": str(d / "no-specs"),
                "RECALL_SKILLS_DIR": str(d / "no-skills"),
                "RECALL_SPECS_INDEX": str(d / "no-specs.txt"),
                "RECALL_HUB_INDEX": str(d / "no-hub.json"),
                "RECALL_CONNECTOR_DIR": str(d / "no-connectors"),
                "RECALL_RECEIPT_DIR": str(d / "no-receipts"),
                "RECALL_ORPHAN_INDEX": str(d / "no-orphans.yaml"),
                "RECALL_PROBE_INDEX": str(d / "recall-home" / "probe-index.json"),
                "RECALL_SURFACED_LOG": str(d / "surfaced.jsonl")})
    return env


def main() -> int:
    print("memory notes")
    with tempfile.TemporaryDirectory(prefix="recall-memory-") as folder:
        d = Path(folder)
        (d / "catalogs").mkdir()
        # Route 2: the agent's per-project folder is a link to the same
        # fixtures route 1 names, so each note must be read once, not twice.
        proj = d / "home" / ".claude" / "projects" / "-some-project"
        proj.mkdir(parents=True)
        (proj / "memory").symlink_to(FIXTURES, target_is_directory=True)
        # Route 3: a folder moved into the personal store's include/ area.
        inc = d / "recall-home" / "include" / "claude" / "projects" / "other" / "memory"
        inc.mkdir(parents=True)
        (inc / "included-note.md").write_text(
            "---\nname: included-note\ndescription: The kiln log lives in the include area\n"
            "type: reference\n---\nKiln readings are logged hourly.\n")
        (inc / ".hidden-note.md").write_text("---\nname: hidden\n---\nzeppelinword\n")
        outside = d / "outside.md"
        outside.write_text("---\nname: outside\n---\nquokkaword\n")
        (inc / "linked-out.md").symlink_to(outside)
        env = env_for(d, os.pathsep.join([str(FIXTURES), str(FIXTURES)]))

        def run(*args: str) -> tuple[int, str, str]:
            p = subprocess.run([sys.executable, str(RECALL), *args],
                               capture_output=True, text=True, env=env)
            return p.returncode, p.stdout, p.stderr

        rc, out, err = run("--stats", "--json")
        stats = json.loads(out) if rc == 0 else {}
        check("--stats exits 0", rc == 0, err)
        check("--stats counts memory notes separately, each folder read once",
              stats.get("memory_note_entries") == 4, str(stats.get("memory_note_entries")))
        check("memory notes are not counted as catalog lessons",
              stats.get("catalog_entries") == 0, str(stats.get("catalog_entries")))
        broken = [b for b in stats.get("unreadable_catalogs", []) if b["catalog"] == "MEMORY"]
        check("the unparsable note is reported once, as MEMORY, with its path",
              len(broken) == 1 and broken[0]["path"].endswith("broken-note.md")
              and broken[0]["error"], str(broken))
        check("a broken note is named on stderr and the rest still serve",
              "broken-note.md" in err and stats.get("memory_note_entries") == 4, err)

        rc, out, _ = run("bucket")
        check("a word only in a description finds the note",
              "nested-metadata-note" in out and "[MEMORY]" in out, out)

        rc, out, _ = run("--id", "nested-metadata-note")
        check("--id exits 0", rc == 0, out)
        check("--id prints the humanised title", "Nested metadata note" in out, out)
        check("--id prints the nested metadata.type and the modified date",
              "memory feedback" in out and "2026-10-14" in out, out)
        check("--id prints the path", str(FIXTURES / "nested-metadata-note.md") in out, out)
        check("--id prints the body in full",
              "nightly export fails with a 403" in out and "reads the old key" in out, out)

        rc, out, _ = run("--id", "Top-level type note")
        check("a top-level type is read and name is the id", rc == 0 and "memory project" in out, out)

        rc, out, _ = run("--id", "plain-note")
        check("a note with no frontmatter is keyed by its file stem",
              rc == 0 and "heron sensor" in out and "memory feedback" not in out, out)

        rc, out, _ = run("--json", "harbour")
        rows = json.loads(out) if rc == 0 else []
        check("a note is a pointer in --json, not a lesson",
              rows and rows[0]["catalog"] == "MEMORY" and rows[0]["pointer"] is True, out)

        rc, out, _ = run("marmalade")
        check("a retired note is out of ordinary search", "retired-note" not in out, out)
        rc, out, _ = run("--include-retired", "marmalade")
        check("--include-retired shows it, marked as history",
              "retired-note" in out and "RETIRED" in out, out)

        rc, out, _ = run("indexwordonly")
        check("MEMORY.md is the index, not a note", "MEMORY" not in out, out)
        rc, out, _ = run("kiln")
        check("a folder under the store's include/ area is read", "included-note" in out, out)
        rc, out, _ = run("zeppelinword quokkaword")
        check("hidden files and links out of the folder are skipped", "no match" in out, out)

    if FAILS:
        print(f"FAIL {len(FAILS)}/{TOTAL} memory-note checks")
        return 1
    print(f"PASS {TOTAL}/{TOTAL} memory-note checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
