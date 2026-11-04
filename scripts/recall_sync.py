#!/usr/bin/env python3
"""Carry a cloud session's memory out of a container that has no durable disk.

    recall_sync.py --session ID --transcript PATH [--final] [--no-digest] [--branch inbox/...]

Writes ONE file, inbox/<host>/<session-id>.json, to the branch `inbox/cloud` of
the personal repo (RECALL_PERSONAL_REMOTE, else the origin of $RECALL_HOME when
that is its own repo). It never pushes anywhere else: the branch must start with
`inbox/`, so main, master and every default branch are refused outright.

The file holds a SCRUBBED digest (digest_transcript.py, then scrub.py), the
friction tally and this session's inbox events and notes. If scrubbing is
uncertain nothing is written or uploaded, and the receipt says so. If the push
cannot happen (no remote, no credential, unreachable, not confirmed private), the
scrubbed payload is kept under $RECALL_HOME/outbox/ and the receipt says exactly
what was not synced and why. It always exits 0: a sync must never break a session.

The local drain reads the branch back (recall_inbox.py). A file with "final":
false is a checkpoint; the drain waits for the final one or for the container to
go quiet (RECALL_INBOX_STALE_HOURS).

Env: RECALL_SYNC_TOKEN | GH_TOKEN | GITHUB_TOKEN   push credential for https remotes
     RECALL_SYNC_TIMEOUT (seconds per git call, default 6)
     RECALL_SYNC_CHECKPOINT_BYTES / _SECONDS        when Stop starts a background sync
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import digest_transcript  # noqa: E402
import friction_events as fe  # noqa: E402
import friction_tally as ft  # noqa: E402
import personal_repo  # noqa: E402
import receipts  # noqa: E402
import scrub  # noqa: E402

BRANCH = "inbox/cloud"
BRANCH_PREFIX = "inbox/"
SCHEMA_VERSION = 1
CHECKPOINT_BYTES = int(os.environ.get("RECALL_SYNC_CHECKPOINT_BYTES") or 512 * 1024)
CHECKPOINT_SECONDS = int(os.environ.get("RECALL_SYNC_CHECKPOINT_SECONDS") or 600)
GIT_TIMEOUT = int(os.environ.get("RECALL_SYNC_TIMEOUT") or 6)
PUSH_ATTEMPTS = 3


# -- state ----------------------------------------------------------------------------
def load_state(home: Path) -> dict:
    try:
        d = json.loads((home / "sync-state.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(home: Path, st: dict) -> None:
    p = home / "sync-state.json"
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    try:
        home.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(st, indent=1))
        os.replace(tmp, p)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()


def checkpoint_due(entry: dict, size: int, now: float, bytes_thr: int, secs_thr: int) -> bool:
    """A checkpoint is due when the transcript grew by bytes_thr, or secs_thr passed with any growth."""
    base_t = entry.get("last_synced_at") or entry.get("first_seen") or now
    base_s = entry.get("last_size") or 0
    return size > base_s and (size - base_s >= bytes_thr or now - base_t >= secs_thr)


# -- payload ----------------------------------------------------------------------------------
def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(s))[:100]


def _outbox(home: Path, host: str, sid: str) -> Path:
    return home / "outbox" / _safe(host) / f"{_safe(sid)}.json"


def _digest(transcript) -> str:
    fd, out = tempfile.mkstemp(suffix=".md")
    os.close(fd)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            digest_transcript.main(str(transcript), out)
        return Path(out).read_text(errors="replace")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(out)


def build_payload(home: Path, sid: str, transcript, host: str, final: bool, with_digest: bool) -> dict:
    prev = {}
    with contextlib.suppress(OSError, ValueError):
        prev = json.loads(_outbox(home, host, sid).read_text())
    have_t = bool(transcript) and os.path.isfile(transcript)
    digest = _digest(transcript) if (with_digest and have_t) else prev.get("digest")
    friction = ft.tally(transcript, sid) if have_t else prev.get("friction", {"events": []})
    events = [e for e in fe.collapse(fe.read_events(home)) if e["session_id"] == sid]
    return {"schema": SCHEMA_VERSION, "session_id": sid, "host": host, "final": bool(final or prev.get("final")),
            "updated_at": fe.now_iso(), "digest": digest, "friction": friction, "events": events}


def _reuse_timestamp(clean: dict, out: Path) -> None:
    """Keep the last outbox timestamp when nothing else changed, so an idle re-sync is
    byte-identical and the push sees "unchanged" instead of committing a new time.
    Compared after scrubbing: the outbox holds scrubbed content, the fresh payload does not."""
    try:
        prev = json.loads(out.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(prev, dict) or "updated_at" not in prev:
        return
    strip = lambda d: {k: v for k, v in d.items() if k != "updated_at"}  # noqa: E731
    if strip(prev) == strip(clean):
        clean["updated_at"] = prev["updated_at"]


# -- git ------------------------------------------------------------------------------------------
def _token() -> str:
    return os.environ.get("RECALL_SYNC_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""


def resolve_remote(home: Path) -> str:
    url = os.environ.get("RECALL_PERSONAL_REMOTE", "").strip()
    if url:
        return url
    if personal_repo.is_own_repo(home):
        return personal_repo.remotes(home).get("origin", "")
    return ""


def _check_remote(url: str, token: str) -> str:
    """"" when the remote may receive a digest, else the reason it may not."""
    is_http = url.startswith("https://")
    if is_http and not token:
        return "no credential for the remote (set RECALL_SYNC_TOKEN to a token that can push to it)"
    prev = os.environ.get("GH_TOKEN")
    if token:
        os.environ["GH_TOKEN"] = token
    try:
        ok, why = personal_repo.judge_remote(url)
    finally:
        if prev is None:
            os.environ.pop("GH_TOKEN", None)
        else:
            os.environ["GH_TOKEN"] = prev
    return "" if ok else f"remote not confirmed private and yours: {why}"


def _git(cwd: Path, url: str, token: str, *args: str):
    cmd = ["git", "-C", str(cwd), "-c", "credential.helper="]
    if token and url.startswith("https://"):
        b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.extraheader=Authorization: Basic {b64}"]
    cmd += ["-c", "user.name=recall", "-c", "user.email=recall@localhost", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _reason(p, token: str) -> str:
    text = ((p.stderr or "") + (p.stdout or "")).strip().splitlines()
    msg = text[-1] if text else f"git exited {p.returncode}"
    return msg.replace(token, "***") if token else msg


def push_file(home: Path, url: str, token: str, rel: str, data: str, branch: str) -> tuple:
    """Commit `data` at `rel` on `branch` of the remote. (True, note) or (False, reason)."""
    if not branch.startswith(BRANCH_PREFIX):
        return False, f"refused: {branch!r} is not an inbox/ branch"
    last = "push did not run"
    for _ in range(PUSH_ATTEMPTS):
        work = Path(tempfile.mkdtemp(prefix="sync-", dir=home))
        try:
            g = lambda *a: _git(work, url, token, *a)  # noqa: E731
            g("init", "-q")
            g("remote", "add", "origin", url)
            f = g("fetch", "-q", "--depth", "1", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
            if f.returncode == 0:
                g("checkout", "-q", "-B", "work", f"origin/{branch}")
                has = True
            elif "couldn't find remote ref" in f.stderr:
                g("symbolic-ref", "HEAD", "refs/heads/work")
                has = False
            else:
                return False, "remote unreachable: " + _reason(f, token)
            target = work / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(data)
            g("add", "--", rel)
            if has and g("diff", "--cached", "--quiet").returncode == 0:
                return True, "unchanged"
            c = g("commit", "-q", "-m", f"inbox: {rel}")
            if c.returncode != 0:
                return False, "commit failed: " + _reason(c, token)
            p = g("push", "-q", "origin", f"HEAD:refs/heads/{branch}")
            if p.returncode == 0:
                return True, "pushed"
            last = "push failed: " + _reason(p, token)
            if not re.search(r"rejected|fetch first|non-fast-forward", p.stderr):
                return False, last
        except subprocess.TimeoutExpired:
            return False, f"timed out after {GIT_TIMEOUT}s per git call"
        except OSError as e:
            return False, f"git unavailable: {e}"
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return False, last


# -- the entry point ----------------------------------------------------------------------------------
def _finish(home, sid, host, status, reason, size, outbox_kept, with_digest):
    st = load_state(home)
    unsynced = st.setdefault("unsynced", {})
    if status == "synced":
        unsynced.pop(sid, None)
        if with_digest:
            cp = st.setdefault("checkpoints", {}).setdefault(sid, {})
            cp.update(last_synced_at=time.time(), last_size=size)
        receipt = f"recall: synced {sid[:8]} -> {BRANCH} ({host})"
        receipts.log(home, "synced", session_id=sid, host=host)
    elif status == "refused":
        receipt = f"recall: refused: {reason}"
    else:
        unsynced[sid] = {"reason": reason, "at": fe.now_iso()}
        kept = f"digest kept at {outbox_kept}" if outbox_kept else "nothing was written or uploaded"
        receipt = f"recall: NOT synced {sid[:8]}: {reason}; {kept}"
        receipts.log(home, "sync_failed", session_id=sid, host=host, reason=reason)
    save_state(home, st)
    return {"status": status, "reason": reason, "receipt": receipt}


def sync_session(home: Path, sid: str, transcript, *, final: bool = False, with_digest: bool = True,
                 branch: str = BRANCH) -> dict:
    host = fe.host_name()
    if not branch.startswith(BRANCH_PREFIX):
        return _finish(home, sid, host, "refused", f"branch {branch!r} refused: recall only pushes to {BRANCH_PREFIX}* branches, never main or a default branch", 0, None, False)
    size = os.path.getsize(transcript) if transcript and os.path.isfile(transcript) else 0
    try:
        payload = build_payload(home, sid, transcript, host, final, with_digest)
        clean, why = scrub.scrub_obj_checked(payload)
    except Exception as e:  # nothing about a failed build is safe to guess at
        return _finish(home, sid, host, "local_only", f"scrubbing uncertain: could not build the payload ({type(e).__name__})", size, None, with_digest)
    if clean is None:
        return _finish(home, sid, host, "local_only", why, size, None, with_digest)
    out = _outbox(home, host, sid)
    _reuse_timestamp(clean, out)
    data = json.dumps(clean, indent=1)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(data)
    except OSError as e:
        return _finish(home, sid, host, "local_only", f"could not write the outbox ({e.strerror})", size, None, with_digest)
    url = resolve_remote(home)
    if not url:
        return _finish(home, sid, host, "local_only", "no remote configured (set RECALL_PERSONAL_REMOTE to your recall-personal repo)", size, out, with_digest)
    token = _token()
    bad = _check_remote(url, token)
    if bad:
        return _finish(home, sid, host, "local_only", bad, size, out, with_digest)
    ok, note = push_file(home, url, token, f"inbox/{_safe(host)}/{_safe(sid)}.json", data, branch)
    if not ok:
        return _finish(home, sid, host, "local_only", note, size, out, with_digest)
    return _finish(home, sid, host, "synced", note, size, out, with_digest)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--session", required=True)
    ap.add_argument("--transcript", default="")
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--no-digest", action="store_true")
    ap.add_argument("--branch", default=BRANCH)
    a = ap.parse_args(argv)
    try:
        r = sync_session(fe.recall_home(), a.session, a.transcript, final=a.final,
                         with_digest=not a.no_digest, branch=a.branch)
        print(r["receipt"])
    except Exception as e:
        print(f"recall: NOT synced {a.session[:8]}: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
