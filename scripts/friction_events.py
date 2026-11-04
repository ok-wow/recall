#!/usr/bin/env python3
"""The inbox: where friction events and model-judged notes are recorded, and said aloud.

    $RECALL_HOME/inbox/events.jsonl      append-only; one JSON event per line
    $RECALL_HOME/friction-state/<sid>.json   per-session cursor and what was already written/announced

An event is {type, signature, count, session_id, host, evidence, ts, ...}. The
schema (event types, note kinds, confidences) is data in inbox_schema.json.
Readers collapse repeats: the same (session, signature) reported at several
counts is one event, the largest count winning.

`process` is the one function the hooks call. It scans only the lines added since
the last call (friction_tally.scan_incremental), writes new events, and - at Stop
- picks at most ONE visible signal for the turn. Its label is a literal state:

    candidate   detected, but NOT written to the inbox (the write failed)
    queued      written to $RECALL_HOME/inbox
    synced      pushed to the inbox/cloud branch

Nothing here says "captured". Every failure degrades to silence or to an honest
label; none of it may break a session.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import friction_tally as ft  # noqa: E402

SCHEMA = json.loads((Path(__file__).resolve().parent / "inbox_schema.json").read_text())
TYPE_RANK = {"retry_loop": 4, "named_pattern": 3, "user_correction": 2, "failed_command": 1, "note": 0}
NOTABLE_TYPES = ("retry_loop", "named_pattern", "user_correction")
WEEK = timedelta(days=7)


def recall_home() -> Path:
    raw = os.environ.get("RECALL_HOME") or os.environ.get("OKWOW_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".recall"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def host_name() -> str:
    raw = os.environ.get("RECALL_HOST") or socket.gethostname() or "unknown"
    return re.sub(r"[^A-Za-z0-9._-]", "_", raw)[:64]


def signals_mode() -> str:
    m = (os.environ.get("RECALL_SIGNALS") or "on").strip().lower()
    return m if m in ("off", "quiet", "on") else "on"


def sync_enabled() -> bool:
    return os.environ.get("RECALL_SYNC") == "1"


def events_path(home: Path) -> Path:
    return home / "inbox" / "events.jsonl"


# -- schema -----------------------------------------------------------------------
def validate_event(ev) -> list:
    if not isinstance(ev, dict):
        return ["event is not an object"]
    errs = [f"missing {k}" for k in SCHEMA["required"] if k not in ev]
    if ev.get("type") not in SCHEMA["types"]:
        errs.append(f"unknown type {ev.get('type')!r}")
    if not isinstance(ev.get("count", 1), int):
        errs.append("count is not an integer")
    if ev.get("type") == "note":
        if ev.get("kind") not in SCHEMA["note_kinds"]:
            errs.append(f"unknown note kind {ev.get('kind')!r}")
        if ev.get("confidence") not in SCHEMA["confidence"]:
            errs.append(f"unknown confidence {ev.get('confidence')!r}")
        if not str(ev.get("text", "")).strip():
            errs.append("note has no text")
    elif "confidence" in ev and ev["confidence"] not in SCHEMA["confidence"]:
        errs.append(f"unknown confidence {ev['confidence']!r}")
    return errs


# -- the file -------------------------------------------------------------------------
def append_events(home: Path, events: list) -> tuple:
    """(True, "") once every line is on disk; (False, reason) otherwise."""
    try:
        for ev in events:
            errs = validate_event(ev)
            if errs:
                return False, "; ".join(errs)
        p = events_path(home)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(data)
        return True, ""
    except OSError as e:
        return False, f"{type(e).__name__}: {e.strerror or e}"


def read_events(home: Path) -> list:
    out = []
    try:
        with open(events_path(home), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict) and ev.get("signature") and ev.get("session_id"):
                    out.append(ev)
    except OSError:
        pass
    return out


def collapse(events: list) -> list:
    """One event per (session, signature): the largest count, the strongest type."""
    best: dict = {}
    for ev in events:
        k = (ev["session_id"], ev["signature"])
        cur = best.get(k)
        rank = (TYPE_RANK.get(ev.get("type"), 0), ev.get("count", 1))
        if cur is None or rank >= (TYPE_RANK.get(cur.get("type"), 0), cur.get("count", 1)):
            best[k] = ev
    return list(best.values())


def _parse_ts(ts):
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# -- per-session state ---------------------------------------------------------------------
def _safe(sid: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(sid))[:100] or "unknown"


def _state_file(home: Path, sid: str) -> Path:
    return home / "friction-state" / f"{_safe(sid)}.json"


@contextlib.contextmanager
def _lock(home: Path, sid: str):
    p = _state_file(home, sid).with_suffix(".lock")
    fd = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + 2.0
        while fd is None:
            try:
                fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    if time.time() - p.stat().st_mtime > 10:
                        p.unlink()
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    break
                time.sleep(0.05)
    except OSError:
        pass
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)
            with contextlib.suppress(OSError):
                p.unlink()


def _load(home: Path, sid: str) -> dict:
    try:
        d = json.loads(_state_file(home, sid).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(home: Path, sid: str, st: dict) -> None:
    p = _state_file(home, sid)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(st))
        os.replace(tmp, p)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _key(ev: dict) -> str:
    return f"{ev['type']}|{ev['signature']}"


# -- the visible signal ------------------------------------------------------------------------
def _ordinal(n: int) -> str:
    return {2: "2nd", 3: "3rd"}.get(n, f"{n}th")


def _recurrence(others: list, now: datetime) -> str:
    recent = [t for t in others if t and now - t <= WEEK]
    if recent:
        return f" ({_ordinal(len(recent) + 1)} time this week)"
    return f" (seen in {len(others)} earlier session{'s' if len(others) != 1 else ''})" if others else ""


def _short(s, n=80) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _signal(home, sid, full, st, error, sync_reason, sync_on) -> str | None:
    mode = signals_mode()
    if mode == "off":
        return None
    prior: dict = {}
    for ev in collapse(read_events(home)):
        if ev["session_id"] != sid:
            prior.setdefault(ev["signature"], []).append(_parse_ts(ev.get("ts")))
    announced = set(st.setdefault("announced", []))
    now = datetime.now(timezone.utc)
    cands = []
    for ev in full:
        sig = ev["signature"]
        if sig in announced:
            continue
        others = prior.get(sig, [])
        if not (others or ev["type"] in NOTABLE_TYPES):
            continue
        if mode == "quiet" and not others and not error:
            continue
        cands.append(((bool(others), TYPE_RANK[ev["type"]], -(ev["evidence"].get("line") or 0)), ev, others))
    if not cands:
        return None
    _, ev, others = max(cands, key=lambda c: c[0])
    k = _key(ev)
    sig = ev["signature"]
    if st["synced"].get(k, -1) >= ev["count"]:
        label = "synced"
    elif k in st["written"]:
        label = "queued"
    else:
        label = "candidate"
    rec = _recurrence(others, now)
    if label == "candidate":
        msg = f"Recall: candidate — {sig}{rec}, detected but not written ({_short(error or 'unknown error')})"
    else:
        msg = f"Recall: {label} — {sig}{rec}, unconfirmed"
        if label == "queued" and sync_on and sync_reason:
            msg += f", not synced: {_short(sync_reason)}"
    if len(cands) > 1:
        msg += f" (+{len(cands) - 1} more)"
    announced.add(sig)
    st["announced"] = sorted(announced)
    return msg


# -- the hooks' entry point ------------------------------------------------------------------------
def process(home: Path, sid: str, transcript, *, announce: bool = False, sync_fn=None) -> dict:
    """Scan new lines, record new events, and (when announce) pick this turn's one signal.

    announce=False (PostToolUse) writes only a first sighting or an escalation;
    growth in a known count waits for announce=True (Stop, SessionEnd).
    """
    out = {"wrote": [], "error": None, "message": None, "result": None}
    with _lock(home, sid) as got:
        if not got:
            return out
        st = _load(home, sid)
        st.setdefault("written", {})
        st.setdefault("synced", {})
        result, st["cursor"] = ft.scan_incremental(transcript, st.get("cursor"), session_id=sid)
        out["result"] = result
        ts, host = now_iso(), host_name()
        full, to_write = [], []
        for e in result["events"]:
            ev = {**e, "session_id": sid, "host": host, "ts": ts, "confidence": "candidate"}
            full.append(ev)
            prev = st["written"].get(_key(ev))
            if prev is None or (announce and ev["count"] > prev):
                to_write.append(ev)
        if to_write:
            ok, err = append_events(home, to_write)
            if ok:
                for ev in to_write:
                    st["written"][_key(ev)] = ev["count"]
                out["wrote"] = to_write
            else:
                out["error"] = err
        sync_reason = None
        if sync_fn and announce and out["wrote"]:
            ok, reason = sync_fn()
            if ok:
                for k, v in st["written"].items():
                    st["synced"][k] = v
            else:
                sync_reason = reason
        if announce:
            out["message"] = _signal(home, sid, full, st, out["error"], sync_reason, bool(sync_fn))
        _save(home, sid, st)
    return out


def light_sync(home: Path, sid: str, transcript):
    def run():
        import recall_sync
        r = recall_sync.sync_session(home, sid, transcript, final=False, with_digest=False)
        return r["status"] == "synced", r.get("reason", "")
    return run


def maybe_checkpoint(home: Path, sid: str, transcript) -> None:
    """At a size/time checkpoint, start a full digest sync in the background."""
    import subprocess

    import recall_sync
    try:
        size = os.path.getsize(transcript)
    except OSError:
        return
    now = time.time()
    state = recall_sync.load_state(home)
    entry = state.setdefault("checkpoints", {}).setdefault(sid, {})
    entry.setdefault("first_seen", now)
    due = recall_sync.checkpoint_due(entry, size, now, recall_sync.CHECKPOINT_BYTES, recall_sync.CHECKPOINT_SECONDS)
    if due and now - entry.get("last_attempt", 0) > 60:
        entry["last_attempt"] = now
        subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "recall_sync.py"),
                          "--session", sid, "--transcript", str(transcript)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    recall_sync.save_state(home, state)


def end_session(home: Path, sid: str, transcript, marker: Path) -> str:
    """SessionEnd: flush counts, attach the tally to the marker, return the one-line receipt."""
    import receipts
    out = process(home, sid, transcript, announce=True)
    result = out["result"] or ft.tally(transcript, sid)
    events = result["events"]
    try:
        m = json.loads(Path(marker).read_text())
        m["friction"] = {"events": events, "commands": result["commands"], "failed": result["failed"]}
        tmp = Path(str(marker) + ".tmp")
        tmp.write_text(json.dumps(m, indent=2))
        os.replace(tmp, marker)
    except (OSError, ValueError):
        pass
    line = f"recall: queued session {sid[:8]} for distillation"
    if events:
        top = ", ".join(f"{e['type']} x{e['count']}" for e in events[:3])
        line += f"; friction: {len(events)} event(s) ({top})"
        if out["error"]:
            line += f"; NOT written to the inbox ({_short(out['error'])})"
    if sync_enabled():
        import recall_sync
        r = recall_sync.sync_session(home, sid, transcript, final=True, with_digest=True)
        line += "; synced to inbox/cloud" if r["status"] == "synced" else f"; NOT synced: {_short(r.get('reason', ''))}"
    receipts.log(home, "queued", session_id=sid, friction=len(events))
    return line
