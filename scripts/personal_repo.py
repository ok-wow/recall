#!/usr/bin/env python3
"""Give one person a private git repo for what Recall stores about them.

Recall keeps each person's memory in RECALL_HOME (~/.recall): the catalogs, the
connectors index, the parking lot, the record of what was surfaced. None of it
has history or a backup, and none of it belongs in a team repo. This makes that
folder its own git repo, commits it, and prints the commands that create a
private GitHub repo under the person's own account and push to it.

    personal_repo.py [plan]        what init would do; changes nothing
    personal_repo.py init --apply  git init, .gitignore, commit, print next steps
    personal_repo.py status        repo? remote? private and yours? unpushed? uncommitted?

It never creates a GitHub repo and never pushes: the person runs those commands.
It does read GitHub, through `gh api`, to check that an existing remote is
private and owned by the person, and it refuses one that is public or belongs to
an organization -- an organization's owners can read every repo in it.

Folders that live elsewhere, such as a Claude Code memory folder, can join the
store (RECALL_PERSONAL_INCLUDE, or --include). Git keeps a link as a link and
not the files behind it, so the files have to move into the store with a link
left behind. This prints those steps; it never moves anyone's files itself.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


def env_path(*names: str, default: Path) -> Path:
    for n in names:
        raw = os.environ.get(n)
        if raw:
            return Path(raw).expanduser()
    return default


HOME = Path.home()
STORE = env_path("RECALL_HOME", "OKWOW_HOME", default=HOME / ".recall")
GH = os.environ.get("RECALL_GH") or "gh"
SELF = shlex.quote(str(Path(__file__).resolve()))
REPO_NAME = "recall-personal"

# Everything in the store is committed except these. Each line says why.
GITIGNORE = """\
# Written by recall's scripts/personal_repo.py.
# Secrets never enter history. The drain reads its token from this file.
drain-credentials
.env
*.env
*.pem
# Logs, caches and locks: rebuilt or rewritten on their own.
*.log
*.lock
drain.lock/
*.tmp
.*.tmp
__pycache__/
.DS_Store
probe-index.json
probe-index.json.error
probe-state/
digests/
# The work queue and its markers: they change every session and carry no lesson.
pending/
processed/
quarantine/
drain-attempts/
auth-down.json
yaml-broken.json
catalog-dirty.json
"""

# A last check before a commit, on what is actually staged. .gitignore is the
# first line of defence; this catches a credential saved under another name.
# Names only for files that ARE credentials by convention: a word like "token"
# in a parked item's name is not one, and a false refusal blocks every commit.
SECRET_NAME = re.compile(r"(^|/)(drain-credentials|\.env|[^/]*\.(env|pem|key|p12)|id_(rsa|ed25519|ecdsa)[^/]*"
                         r"|\.netrc|credentials\.json)$", re.I)
SECRET_TEXT = re.compile(rb"sk-ant-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
                         rb"|AKIA[0-9A-Z]{16}|CLAUDE_CODE_OAUTH_TOKEN=\S{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----")


def git(store: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(store), *args], capture_output=True, text=True, check=check)


def is_own_repo(store: Path) -> bool:
    p = git(store, "rev-parse", "--show-toplevel")
    return p.returncode == 0 and Path(p.stdout.strip()).resolve() == store.resolve()


def enclosing_repo(store: Path) -> Path | None:
    """The repo the store sits inside, when that repo is not the store itself."""
    p = git(store, "rev-parse", "--show-toplevel")
    if p.returncode != 0:
        return None
    top = Path(p.stdout.strip()).resolve()
    return None if top == store.resolve() else top


# ------------------------------------------------------------------ remotes --
def remotes(store: Path) -> dict[str, str]:
    if not is_own_repo(store):
        return {}
    out = {}
    for line in git(store, "remote", "-v").stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out.setdefault(parts[0], parts[1])
    return out


GITHUB = re.compile(r"^(?:git@github\.com:|ssh://git@github\.com/|https://(?:[^@/]+@)?github\.com/)"
                    r"([^/]+)/(.+?)(?:\.git)?/?$")


def gh_json(*args: str) -> tuple[dict | None, str]:
    try:
        p = subprocess.run([GH, "api", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"could not run {GH}: {e}"
    if p.returncode != 0:
        return None, (p.stderr.strip().splitlines() or ["gh api failed"])[-1]
    try:
        return json.loads(p.stdout), ""
    except ValueError:
        return None, "gh api returned something that is not JSON"


def judge_remote(url: str) -> tuple[bool, str]:
    """(safe, why). Safe means private and owned by the person running this."""
    if url.startswith("file://") or url.startswith("/") or url.startswith("~") or url.startswith("."):
        return True, "a folder on this machine"
    m = GITHUB.match(url)
    if not m:
        return False, ("not a GitHub remote, so nothing here can check who can read it. "
                       "Use a private GitHub repo under your own account, or a folder on this machine")
    owner, name = m.groups()
    me, err = gh_json("user")
    if me is None:
        return False, f"cannot confirm it is private and yours ({err}). Run `gh auth login`, then try again"
    repo, err = gh_json(f"repos/{owner}/{name}")
    if repo is None:
        return False, f"cannot read {owner}/{name} on GitHub ({err})"
    o = repo.get("owner") or {}
    if o.get("type") == "Organization":
        return False, (f"it belongs to the organization {o.get('login')}. A personal store there is "
                       f"readable by that organization's owners, so it has to live under your own account")
    visibility = repo.get("visibility") or ("private" if repo.get("private") else "public")
    if visibility != "private" or not repo.get("private"):
        return False, f"{owner}/{name} is {visibility}. Your memory has to be in a private repo"
    if (o.get("login") or "").lower() != str(me.get("login", "")).lower():
        return False, f"it belongs to {o.get('login')}, not to you ({me.get('login')})"
    return True, f"private, owned by {me.get('login')}"


# ----------------------------------------------------------------- contents --
def pending_files(store: Path) -> list[str]:
    """Files the next commit would take, with this script's ignore rules applied."""
    with tempfile.TemporaryDirectory() as tmp:
        rules = Path(tmp) / "ignore"
        rules.write_text(GITIGNORE)
        if is_own_repo(store):
            base = ["git", "-C", str(store)]
        else:
            # A throwaway git dir reads the folder without writing to it.
            subprocess.run(["git", "init", "-q", "--bare", f"{tmp}/g"], capture_output=True, check=True)
            base = ["git", f"--git-dir={tmp}/g", f"--work-tree={store}", "-C", str(store)]
        p = subprocess.run([*base, "ls-files", "-o", "-m", "--exclude-standard", f"--exclude-from={rules}"],
                           capture_output=True, text=True)
    files = sorted(set(p.stdout.splitlines()))
    if not (store / ".gitignore").exists() and ".gitignore" not in files:
        files.append(".gitignore")
    return files


def secrets_in(store: Path, files: list[str]) -> list[str]:
    bad = []
    for f in files:
        path = store / f
        if SECRET_NAME.search(f):
            bad.append(f"{f} (its name looks like a credential)")
            continue
        try:
            if path.is_file() and not path.is_symlink() and SECRET_TEXT.search(path.read_bytes()):
                bad.append(f"{f} (it contains something shaped like a key or token)")
        except OSError:
            pass
    return bad


def outside_links(store: Path) -> list[str]:
    """Links in the store that point out of it. Git stores the link, not the files."""
    out, root = [], store.resolve()
    for d, dirs, files in os.walk(store):          # does not descend into links
        dirs[:] = [x for x in dirs if x != ".git"]
        for name in sorted(dirs + files):
            p = Path(d) / name
            if p.is_symlink():
                target = p.resolve()
                if root not in target.parents and target != root:
                    out.append(f"{p.relative_to(store)} -> {target}")
    return sorted(out)


def includes(extra: list[str]) -> list[Path]:
    raw = [s for s in (os.environ.get("RECALL_PERSONAL_INCLUDE") or "").split(os.pathsep) if s]
    return [Path(s).expanduser() for s in raw + extra]


def include_home(store: Path, src: Path) -> Path:
    """Where an outside folder goes inside the store: include/<its path under $HOME>."""
    try:
        rel = src.absolute().relative_to(HOME)
    except ValueError:
        rel = Path(*src.absolute().parts[1:])
    return store / "include" / Path(*[part.lstrip(".") or part for part in rel.parts])


def include_report(store: Path, paths: list[Path]) -> list[str]:
    lines = []
    for src in paths:
        dest = include_home(store, src)
        if src.is_symlink() and (src.resolve() == dest.resolve() or store.resolve() in src.resolve().parents):
            lines.append(f"  {src}  included (a link into the store)")
        elif not src.exists():
            lines.append(f"  {src}  not found")
        else:
            q = shlex.quote
            lines += [f"  {src}  not in the store yet. To add it, move it in and leave a link behind:",
                      f"      mkdir -p {q(str(dest.parent))}",
                      f"      mv {q(str(src))} {q(str(dest))}",
                      f"      ln -s {q(str(dest))} {q(str(src))}"]
    return lines


def push_commands(store: Path) -> list[str]:
    return [
        "  1. gh auth status",
        "       (check that gh is signed in to YOUR account)",
        f'  2. gh repo create "$(gh api user --jq .login)/{REPO_NAME}" --private '
        f"--source {shlex.quote(str(store))} --remote origin --push",
    ]


# ------------------------------------------------------------------ commands --
def refusal(store: Path) -> str | None:
    if not store.is_dir():
        return f"there is no store at {store} yet. Install Recall first, or set RECALL_HOME."
    outer = enclosing_repo(store)
    if outer:
        return (f"{store} is inside the git repo at {outer}. A personal store must be its own repo, "
                f"or its history lands in that one.")
    for name, url in remotes(store).items():
        ok, why = judge_remote(url)
        if not ok:
            return f"remote '{name}' ({url}) cannot hold a personal store: {why}."
    return None


def cmd_plan(store: Path, extra: list[str]) -> int:
    print(f"personal store plan for {store}  (nothing is changed)\n")
    stop = refusal(store)
    if stop:
        print(f"  init would refuse: {stop}")
        return 0
    files = pending_files(store)
    print(f"  1. {'already a git repo' if is_own_repo(store) else 'make it a git repo'}")
    print("  2. write .gitignore: credentials, logs, caches, locks, and the work queue stay out")
    tops = sorted({f.split('/')[0] for f in files})
    print(f"  3. commit {len(files)} file(s): {', '.join(tops) if tops else 'nothing new'}")
    bad = secrets_in(store, files)
    for b in bad:
        print(f"     init would refuse to commit {b}")
    links = outside_links(store)
    for ln in links:
        print(f"     note: {ln} is a link. Git keeps the link, not the files it points at.")
    if remotes(store):
        print("  4. remote: " + ", ".join(f"{n} {u}" for n, u in remotes(store).items()) + " (checked: private and yours)")
    else:
        print("  4. no remote yet. After init, run these two commands yourself:")
        print("\n".join("   " + c for c in push_commands(store)))
    inc = include_report(store, includes(extra))
    if inc:
        print("\n  folders from elsewhere (RECALL_PERSONAL_INCLUDE / --include):")
        print("\n".join(inc))
    print(f"\n  run it:  python3 {SELF} init --apply")
    return 0


def cmd_init(store: Path, extra: list[str]) -> int:
    stop = refusal(store)
    if stop:
        print(f"refused: {stop}", file=sys.stderr)
        return 1
    if not is_own_repo(store):
        git(store, "init", "-q", check=True)
        git(store, "symbolic-ref", "HEAD", "refs/heads/main", check=True)
        print(f"  made {store} a git repo")
    ignore = store / ".gitignore"
    have = ignore.read_text().splitlines() if ignore.exists() else []
    missing = [ln for ln in GITIGNORE.splitlines() if ln and not ln.startswith("#") and ln not in have]
    if not ignore.exists():
        ignore.write_text(GITIGNORE)
        print("  wrote .gitignore")
    elif missing:
        with ignore.open("a") as fh:
            fh.write("\n# Added by recall's personal_repo.py\n" + "\n".join(missing) + "\n")
        print(f"  added {len(missing)} line(s) to .gitignore")

    git(store, "add", "--", ".", check=True)
    staged = [f for f in git(store, "diff", "--cached", "--name-only").stdout.splitlines() if f]
    bad = secrets_in(store, staged)
    if bad:
        git(store, "reset", "-q")
        print("refused: these look like credentials, so nothing was committed:", file=sys.stderr)
        for b in bad:
            print(f"  {b}", file=sys.stderr)
        print("Move them out of the store, or add them to .gitignore, then run this again.", file=sys.stderr)
        return 1
    if not staged:
        print("  nothing new to commit")
    else:
        p = git(store, "commit", "-q", "-m", f"recall: personal store, {len(staged)} file(s)")
        if p.returncode != 0:
            print(f"refused: git could not commit: {p.stderr.strip()}", file=sys.stderr)
            print("Set your name and email once:  git config --global user.name 'You'  "
                  "and  git config --global user.email you@example.com", file=sys.stderr)
            return 1
        print(f"  committed {len(staged)} file(s)")

    for ln in outside_links(store):
        print(f"  note: {ln} is a link. Git keeps the link, not the files it points at.")
    inc = include_report(store, includes(extra))
    if inc:
        print("\n  folders from elsewhere:")
        print("\n".join(inc))
    if remotes(store):
        first = next(iter(remotes(store)))
        print(f"\n  push when you are ready:  git -C {shlex.quote(str(store))} push -u {first} HEAD")
    else:
        print("\n  Now create the private repo under your own account and push. Run these yourself:")
        print("\n".join(push_commands(store)))
    return 0


def cmd_status(store: Path, extra: list[str]) -> int:
    print(f"personal store  {store}")
    if not store.is_dir():
        print("  not found")
        return 0
    if not is_own_repo(store):
        outer = enclosing_repo(store)
        print(f"  git repo      no{f' (inside {outer})' if outer else ''}")
        print(f"  next          python3 {SELF} init --apply")
        return 0
    has_head = git(store, "rev-parse", "--verify", "-q", "HEAD").returncode == 0
    total = int(git(store, "rev-list", "--count", "HEAD").stdout.strip() or 0) if has_head else 0
    print(f"  git repo      yes, {total} commit(s)")
    rs = remotes(store)
    if not rs:
        print("  remote        none")
        print(f"  unpushed      {total} commit(s), nothing is backed up yet")
    for name, url in rs.items():
        ok, why = judge_remote(url)
        print(f"  remote        {name} {url}")
        print(f"  private+yours {'yes' if ok else 'NO'}: {why}")
    if rs:
        ahead = git(store, "rev-list", "--count", "HEAD", "--not", "--remotes").stdout.strip() if has_head else "0"
        print(f"  unpushed      {ahead or 0} commit(s) (as of the last push or fetch)")
    dirty = [ln for ln in git(store, "status", "--porcelain", "--untracked-files=all").stdout.splitlines() if ln]
    print(f"  uncommitted   {len(dirty)} file(s)")
    for ln in outside_links(store):
        print(f"  note          {ln} is a link; its files are not in this repo")
    inc = include_report(store, includes(extra))
    if inc:
        print("  includes")
        print("\n".join("  " + ln for ln in inc))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", nargs="?", default="plan", choices=("plan", "init", "status"))
    ap.add_argument("--apply", action="store_true", help="with init: make the changes")
    ap.add_argument("--dir", type=Path, default=STORE, help=f"the store (default {STORE})")
    ap.add_argument("--include", action="append", default=[], metavar="PATH",
                    help="a folder elsewhere to bring into the store (repeatable)")
    a = ap.parse_args(argv)
    store = a.dir.expanduser()
    if a.command == "status":
        return cmd_status(store, a.include)
    if a.command == "init" and a.apply:
        return cmd_init(store, a.include)
    return cmd_plan(store, a.include)


if __name__ == "__main__":
    raise SystemExit(main())
