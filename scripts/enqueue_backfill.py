#!/usr/bin/env python3
"""Batch-backfill enqueuer for okwow-compound.

Enumerates historical Claude Code session transcripts and writes a pending
marker for each one worth compounding — so the EXISTING /okwow-compound engine
drains them through the real router (artifacts_registry.yaml → FAILURE_MODES /
PROCESS_FAILURES / skill rules / orphanage). No second memory pile, no new
taxonomy.

Markers land in a SEPARATE backfill queue (~/.claude/.okwow-compound-backfill/)
rather than the live pending dir, so bulk history does NOT flood the
SessionStart reminder. The engine drains the backfill queue in throttled
batches (see SKILL.md § Batch backfill mode).

Dates come from the transcript's own first/last entry timestamps, never file
mtime (per the hand-curated-dates-over-mtime learning).

Usage:
    enqueue_backfill.py            # dry-run: report what WOULD be enqueued
    enqueue_backfill.py --apply    # actually write markers
"""
import json
import os
import sys
from pathlib import Path

# Historical session dirs to mine: every project folder the host keeps, or the
# ones named in RECALL_SESSIONS_DIRS (colon-separated).
_env_dirs = os.environ.get("RECALL_SESSIONS_DIRS", "")
SESSIONS_DIRS = ([Path(d).expanduser() for d in _env_dirs.split(":") if d]
                 or sorted(p for p in (Path.home() / ".claude/projects").glob("*") if p.is_dir()))
BACKFILL_DIR = Path.home() / ".claude/.okwow-compound-backfill"
PENDING_DIR = Path.home() / ".claude/.okwow-compound-pending"
LEDGER = BACKFILL_DIR / "_enqueued.json"          # session_ids ever enqueued

# Same 80KB bar the SessionEnd hook uses — below this a transcript rarely holds
# a durable, non-trivial learning worth a full distill pass.
MIN_BYTES = 80_000


def last_timestamp(jsonl_path: Path) -> str | None:
    """ISO timestamp of the transcript's final entry (its real end time)."""
    ts = None
    try:
        with open(jsonl_path) as f:
            for raw in f:
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if entry.get("timestamp"):
                    ts = entry["timestamp"]
    except Exception:
        return None
    return ts


def load_ledger() -> set:
    if LEDGER.exists():
        try:
            return set(json.loads(LEDGER.read_text()))
        except Exception:
            pass
    return set()


def already_queued_or_done(session_id: str, ledger: set) -> bool:
    return (
        session_id in ledger
        or (BACKFILL_DIR / f"{session_id}.json").exists()
        or (PENDING_DIR / f"{session_id}.json").exists()
    )


def main() -> None:
    apply = "--apply" in sys.argv
    ledger = load_ledger()

    transcripts = []
    for d in SESSIONS_DIRS:
        if d.exists():
            transcripts.extend(d.glob("*.jsonl"))

    to_enqueue, skipped_small, skipped_dup = [], 0, 0
    for path in sorted(transcripts, key=lambda p: p.stat().st_mtime):
        sid = path.stem
        size = path.stat().st_size
        if size < MIN_BYTES:
            skipped_small += 1
            continue
        if already_queued_or_done(sid, ledger):
            skipped_dup += 1
            continue
        to_enqueue.append((sid, path, size))

    print(f"scanned   : {len(transcripts)} transcripts across {len(SESSIONS_DIRS)} dirs")
    print(f"skip <80KB: {skipped_small}")
    print(f"skip dup  : {skipped_dup} (already queued / pending / done)")
    print(f"to enqueue: {len(to_enqueue)}")

    if not apply:
        print("\n(dry-run — pass --apply to write markers)")
        for sid, path, size in to_enqueue[:10]:
            print(f"  + {sid[:8]}  {size//1024:>5}KB  {path.parent.name}")
        if len(to_enqueue) > 10:
            print(f"  … and {len(to_enqueue) - 10} more")
        return

    BACKFILL_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for sid, path, size in to_enqueue:
        marker = {
            "session_id": sid,
            "transcript_path": str(path),
            "ended_at": last_timestamp(path) or "unknown",
            "transcript_size_bytes": size,
            "reason": "backfill-historical-transcript",
        }
        (BACKFILL_DIR / f"{sid}.json").write_text(json.dumps(marker, indent=2))
        ledger.add(sid)
        written += 1
    LEDGER.write_text(json.dumps(sorted(ledger), indent=2))
    print(f"\nwrote {written} backfill markers to {BACKFILL_DIR}")
    print("drain them with /okwow-compound (it processes the backfill queue in "
          "throttled batches — see SKILL.md § Batch backfill mode)")


if __name__ == "__main__":
    main()
