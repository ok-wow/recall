#!/usr/bin/env python3
"""personal_repo.py: a private repo for one person's store, and nothing else.

Every case runs the real script against a throwaway HOME, with a fake `gh` that
logs each call and answers from a fixture, and with remotes that are either a
local bare repo or GitHub URLs that are never contacted. No network: the script
never fetches or pushes, and `gh` is never the real one.

What must hold:
  - plan changes nothing;
  - init commits the store with credentials, logs, caches, locks and the queue
    left out, and prints the create-and-push commands instead of running them;
  - a remote that is public, owned by an organization, owned by someone else,
    or cannot be checked is refused before anything changes;
  - a credential that slipped past .gitignore stops the commit;
  - a folder from elsewhere is never moved, only described;
  - status reports repo, remote, private-and-yours, unpushed and uncommitted.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "personal_repo.py"
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
state = json.load(open(os.environ["FAKE_GH_STATE"]))
a = sys.argv[1:]
if a[:2] == ["api", "user"]:
    if not state.get("login"):
        sys.exit("gh: not logged in to any hosts")
    print(json.dumps({"login": state["login"]}))
elif a[:1] == ["api"] and a[1].startswith("repos/"):
    r = state.get("repos", {}).get(a[1][len("repos/"):])
    if r is None:
        sys.exit("gh: Not Found (HTTP 404)")
    print(json.dumps(r))
else:
    sys.exit("fake gh: unexpected call " + " ".join(a))
'''

fails: list[str] = []
ran = [0]


def check(label: str, ok: bool, detail: str = "") -> None:
    ran[0] += 1
    if not ok:
        fails.append(f"{label}{(' — ' + str(detail)[-400:]) if detail else ''}")


class World:
    """A throwaway HOME with a populated ~/.recall, a fake gh, and isolated git."""

    def __init__(self) -> None:
        self.base = Path(tempfile.mkdtemp())
        self.home = self.base / "home"
        self.store = self.home / ".recall"
        for d in ("catalogs", "connectors", "lot/items", "pending", "processed", "digests",
                  "probe-state", "drain.lock", "quarantine"):
            (self.store / d).mkdir(parents=True)
        files = {
            "catalogs/FAILURE_MODES.yaml": "- id: kept\n", "catalogs/DECISIONS.yaml": "[]\n",
            "connectors/slack.jsonl": "{}\n", "lot/items/rotate-the-api-token.json": "{}\n",
            "surfaced.jsonl": "{}\n", "due-checks.json": "[]\n",
            "drain.log": "log\n", "drain-credentials": "CLAUDE_CODE_OAUTH_TOKEN=abc\n",
            "probe-index.json": "{}\n", "pending/s1.json": "{}\n", "processed/s0.json": "{}\n",
            "digests/s2.md": "d\n", "probe-state/s1.json": "[]\n", "drain.lock/pid": "1\n",
            "quarantine/s3.json": "{}\n",
        }
        for rel, text in files.items():
            (self.store / rel).write_text(text)
        (self.base / "bin").mkdir()
        self.gh = self.base / "bin" / "gh"
        self.gh.write_text(FAKE_GH)
        self.gh.chmod(0o755)
        self.gh_log = self.base / "gh.log"
        self.gh_log.write_text("")
        self.gh_state = self.base / "gh.json"
        self.set_gh(login="alice")
        (self.base / "gitconfig").write_text("")

    def set_gh(self, login=None, repos=None) -> None:
        self.gh_state.write_text(json.dumps({"login": login, "repos": repos or {}}))

    def env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("RECALL_", "OKWOW_", "GIT_"))}
        env.update(HOME=str(self.home), FAKE_GH_LOG=str(self.gh_log), FAKE_GH_STATE=str(self.gh_state),
                   GIT_CONFIG_GLOBAL=str(self.base / "gitconfig"), GIT_CONFIG_NOSYSTEM="1",
                   GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="t@example.com",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="t@example.com")
        env["RECALL_GH"] = str(self.gh)        # never the real gh
        env.update(extra)
        return env

    def run(self, *args: str, **extra) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              env=self.env(**extra), timeout=60)

    def git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(cwd or self.store), *args], capture_output=True,
                              text=True, env=self.env())

    def commits(self) -> int:
        p = self.git("rev-list", "--count", "HEAD")
        return int(p.stdout.strip()) if p.returncode == 0 else 0

    def tree(self) -> dict:
        return {str(p.relative_to(self.base)): p.lstat().st_mtime_ns for p in self.base.rglob("*")
                if "gh.log" not in p.name}

    def close(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)


def gh_calls(w: World) -> list[str]:
    return [ln for ln in w.gh_log.read_text().splitlines() if ln]


# ---------------------------------------------------- plan, init, status: happy --
w = World()
try:
    before = w.tree()
    p = w.run()
    check("plan exits 0", p.returncode == 0, p.stderr)
    check("plan changes nothing", w.tree() == before)
    check("plan names the files it would leave out by counting only the rest",
          "commit 7 file(s)" in p.stdout, p.stdout)
    p = w.run("init")
    check("init without --apply is a plan too", w.tree() == before and "nothing is changed" in p.stdout)

    p = w.run("init", "--apply")
    check("init --apply exits 0", p.returncode == 0, p.stdout + p.stderr)
    check("the store is its own git repo", (w.store / ".git").is_dir())
    check("one commit", w.commits() == 1, w.commits())
    tracked = set(w.git("ls-files").stdout.split())
    for f in ("catalogs/FAILURE_MODES.yaml", "catalogs/DECISIONS.yaml", "connectors/slack.jsonl",
              "lot/items/rotate-the-api-token.json", "surfaced.jsonl", "due-checks.json", ".gitignore"):
        check(f"committed: {f}", f in tracked, sorted(tracked))
    for f in ("drain.log", "drain-credentials", "probe-index.json", "pending/s1.json", "processed/s0.json",
              "digests/s2.md", "probe-state/s1.json", "drain.lock/pid", "quarantine/s3.json"):
        check(f"left out: {f}", f not in tracked)
    check("prints the create command under the person's own account, private",
          f'gh repo create "$(gh api user --jq .login)/recall-personal" --private --source {w.store} '
          f"--remote origin --push" in p.stdout, p.stdout)
    check("prints gh auth status as the first step", "gh auth status" in p.stdout)
    check("never called gh: there is no remote to check", gh_calls(w) == [], gh_calls(w))

    p = w.run("init", "--apply")
    check("a second init is a no-op", p.returncode == 0 and "nothing new to commit" in p.stdout
          and w.commits() == 1, p.stdout)
    (w.store / "catalogs" / "DECISIONS.yaml").write_text("- id: new\n")
    w.run("init", "--apply")
    check("a change makes a second commit", w.commits() == 2, w.commits())

    p = w.run("status")
    check("status: a repo with 2 commits", "yes, 2 commit(s)" in p.stdout, p.stdout)
    check("status: no remote, so everything is unpushed", "remote        none" in p.stdout
          and "2 commit(s), nothing is backed up yet" in p.stdout, p.stdout)
    (w.store / "connectors" / "fathom.jsonl").write_text("{}\n")
    check("status: counts an uncommitted file", "uncommitted   1 file(s)" in w.run("status").stdout)

    # A local bare repo is a remote on this machine: private by nature.
    bare = w.base / "backup.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=w.env())
    w.git("remote", "add", "origin", str(bare))
    p = w.run("init", "--apply")
    check("a local folder remote is accepted", p.returncode == 0, p.stderr)
    check("init never pushes", w.git("for-each-ref", cwd=bare).stdout.strip() == "")
    check("init prints the push for the person to run", "push -u origin HEAD" in p.stdout, p.stdout)
    p = w.run("status")
    check("status: local remote counts as private and yours", "private+yours yes" in p.stdout, p.stdout)
    check("status: 3 unpushed before the person pushes", "unpushed      3 commit(s)" in p.stdout, p.stdout)
    w.git("push", "-q", "-u", "origin", "main")
    check("status: 0 unpushed after the person pushes",
          "unpushed      0 commit(s)" in w.run("status").stdout)
finally:
    w.close()


# ------------------------------------------------------------ remote refusals --
def remote_case(label: str, repo: dict | None, login: str | None, want_ok: bool, needle: str,
                url: str = "git@github.com:{owner}/recall-personal.git", gh: str | None = None) -> None:
    w = World()
    try:
        w.run("init", "--apply")
        owner = (repo or {}).get("owner", {}).get("login", "alice")
        u = url.format(owner=owner)
        w.git("remote", "add", "origin", u)
        w.set_gh(login=login, repos={f"{owner}/recall-personal": repo} if repo else {})
        (w.store / "catalogs" / "DECISIONS.yaml").write_text("- id: after-remote\n")
        ignore_before = (w.store / ".gitignore").read_text()
        extra = {"RECALL_GH": gh} if gh else {}
        p = w.run("init", "--apply", **extra)
        if want_ok:
            check(f"[{label}] accepted", p.returncode == 0 and w.commits() == 2, p.stdout + p.stderr)
        else:
            check(f"[{label}] refused", p.returncode == 1, p.stdout + p.stderr)
            check(f"[{label}] says why", needle in p.stderr, p.stderr)
            check(f"[{label}] nothing changed", w.commits() == 1
                  and (w.store / ".gitignore").read_text() == ignore_before)
        s = w.run("status", **extra).stdout
        check(f"[{label}] status agrees", ("private+yours yes" in s) == want_ok, s)
        check(f"[{label}] gh was only read", all(c.startswith("api ") for c in gh_calls(w)), gh_calls(w))
    finally:
        w.close()


def gh_repo(owner: str, kind: str = "User", private: bool = True) -> dict:
    return {"private": private, "visibility": "private" if private else "public",
            "owner": {"login": owner, "type": kind}}


remote_case("private, yours", gh_repo("alice"), "alice", True, "")
remote_case("owned by an organization", gh_repo("acme", "Organization"), "alice", False,
            "organization's owners")
remote_case("public", gh_repo("alice", private=False), "alice", False, "is public")
remote_case("someone else's", gh_repo("bob"), "alice", False, "belongs to bob, not to you")
remote_case("gh not signed in", gh_repo("alice"), None, False, "gh auth login")
remote_case("gh missing", gh_repo("alice"), "alice", False, "cannot confirm", gh="/nonexistent/gh")
remote_case("a host nothing can check", None, "alice", False, "not a GitHub remote",
            url="https://gitlab.example.com/alice/recall-personal.git")


# ------------------------------------------------------------ other refusals --
w = World()
try:
    subprocess.run(["git", "init", "-q", str(w.home)], check=True, env=w.env())
    p = w.run("init", "--apply")
    check("a store inside another repo is refused", p.returncode == 1 and "inside the git repo" in p.stderr,
          p.stderr)
    check("…and is not made a repo", not (w.store / ".git").exists())
finally:
    w.close()

w = World()
try:
    token = "ghp_" + "A1b2C3d4" * 5
    (w.store / "connectors" / "notes.jsonl").write_text(f'{{"text": "{token}"}}\n')
    # .gitignore keeps *.pem out before anything is staged; a name it does not
    # list has to be caught by the scan of what is staged.
    (w.store / "connectors" / "server.pem").write_text("pem\n")
    (w.store / "connectors" / "server.key").write_text("key\n")
    p = w.run("init", "--apply")
    check("a token inside a file stops the commit", p.returncode == 1 and "notes.jsonl" in p.stderr, p.stderr)
    check("a key file stops the commit", "server.key" in p.stderr, p.stderr)
    check("a file .gitignore keeps out is never staged, so it is not reported",
          "server.pem" not in p.stderr, p.stderr)
    check("…and nothing is committed or left staged", w.commits() == 0
          and w.git("diff", "--cached", "--name-only").stdout.strip() == "")
    check("a parked item with 'token' in its name is not a credential",
          "rotate-the-api-token" not in p.stderr, p.stderr)
finally:
    w.close()

w = World()
try:
    shutil.rmtree(w.store)
    p = w.run("init", "--apply")
    check("no store yet is refused", p.returncode == 1 and "no store" in p.stderr, p.stderr)
finally:
    w.close()


# ---------------------------------------------------- folders from elsewhere --
w = World()
try:
    mem = w.home / ".claude" / "projects" / "-Users-a-dev" / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("m\n")
    dest = w.store / "include" / "claude" / "projects" / "-Users-a-dev" / "memory"
    for label, args, extra in (("--include", ("--include", str(mem)), {}),
                               ("RECALL_PERSONAL_INCLUDE", (), {"RECALL_PERSONAL_INCLUDE": str(mem)})):
        p = w.run(*args, **extra)
        check(f"[{label}] prints the move", f"mv {mem} {dest}" in p.stdout, p.stdout)
        check(f"[{label}] prints the link left behind", f"ln -s {dest} {mem}" in p.stdout, p.stdout)
    w.run("init", "--apply", "--include", str(mem))
    check("the folder is never moved by the script", mem.is_dir() and not mem.is_symlink())
    # the person follows the printed steps
    dest.parent.mkdir(parents=True)
    shutil.move(str(mem), str(dest))
    mem.symlink_to(dest)
    s = w.run("status", "--include", str(mem)).stdout
    check("after the person moves it, status says included", "included" in s, s)
    w.run("init", "--apply")
    check("…and the next init commits its files",
          "include/claude/projects/-Users-a-dev/memory/MEMORY.md" in w.git("ls-files").stdout)

    # A link inside the store that points out keeps the link, not the files.
    elsewhere = w.base / "team-catalogs"
    elsewhere.mkdir()
    (w.store / "shared").symlink_to(elsewhere)
    check("a link out of the store is flagged", "is a link" in w.run().stdout)
finally:
    w.close()

if fails:
    print(f"FAIL {len(fails)} of {ran[0]} check(s):")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASS {ran[0]}/{ran[0]} personal-repo checks (fake gh, fake remotes, no network)")
