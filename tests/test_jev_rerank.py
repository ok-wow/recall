#!/usr/bin/env python3
"""The judged rerank of a pull — scripts/jev.py and recall.py's --rerank.

Every case runs against a real HTTP server on localhost, not a patched
urlopen. The failures worth catching here are in the wire: a header that is
not sent, a body the gateway would reject, a 429 that is not retried, an
answer shape that is read too generously. A mock that returns whatever the
test wants cannot see any of them.

Nothing here reaches the network, and nothing reads the real corpus or the
real retrieval log.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT_DIR = Path(os.environ.get("RECALL_SKILL_DIR")
                  or Path(__file__).resolve().parent.parent / "scripts")
RECALL = SCRIPT_DIR / "recall.py"
sys.path.insert(0, str(SCRIPT_DIR))

# Two entries whose BM25 order is knowable and whose judged order we invert.
FM = [
    {"id": "alpha-cache-warms-on-first-request",
     "what": "The alpha cache warms on the first request and the first caller pays for it.",
     "fix_pattern": "Warm the cache at boot.",
     "probe_when": ["`alpha` cache"],
     "recurrences": 0},
    {"id": "beta-retry-storms-on-timeout",
     # Shares "request" with alpha on purpose: BM25 has to return BOTH or there
     # is nothing for a reranker to reorder, and the test proves nothing.
     "what": "A beta retry storm follows every upstream request timeout because each "
             "client retries the request alone.",
     "fix_pattern": "Back off with jitter.",
     "probe_when": ["`beta` retry"],
     "recurrences": 0},
]


class Gateway:
    """A stand-in for the judge. `policy(lessons) -> (status, payload)`."""

    def __init__(self, policy):
        self.policy = policy
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):                      # noqa: N802 (stdlib's name)
                body = json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length", 0))) or b"{}")
                outer.requests.append({"body": body,
                                       "auth": self.headers.get("Authorization", "")})
                status, payload = outer.policy(body, len(outer.requests))
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):              # keep the suite's output clean
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def answers(scores: dict[str, float]) -> dict:
    return {"answers": {n: {"noul": v} for n, v in scores.items()}}


def catalog_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="recalljev-"))
    import yaml
    (d / "FAILURE_MODES.yaml").write_text(yaml.safe_dump(FM, sort_keys=False))
    (d / "PROCESS_FAILURES.yaml").write_text(yaml.safe_dump([], sort_keys=False))
    return d


def run(d: Path, *args: str, url: str | None = None, key: str | None = "test-key",
        log: Path | None = None, extra: dict | None = None) -> tuple[int, str]:
    env = {**os.environ,
           "RECALL_CATALOG_DIR": str(d),
           # Pin the last unpinned path too, or every fixture corpus silently
           # gains the real rules from ~/.claude/skills.
           "RECALL_SKILLS_DIR": str(Path(__file__).resolve().parent / "fixtures" / "no-skills"),
           "RECALL_PROBE_INDEX": str(d / "nonexistent-index.json"),
           "RECALL_SURFACED_LOG": str(log or d / "surfaced.jsonl"),
           # Short, so the retry cases do not stall the suite.
           "RECALL_JEV_TIMEOUT": "5"}
    env.pop("RECALL_RERANK", None)
    env.pop("AI_GATEWAY_API_KEY", None)
    if url:
        env["RECALL_JEV_URL"] = url
    if key:
        env["AI_GATEWAY_API_KEY"] = key
    env.update(extra or {})
    p = subprocess.run([sys.executable, str(RECALL), *args],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def order(out: str) -> list[str]:
    """Entry ids in the order they were printed."""
    return [ln.split()[0] for ln in out.splitlines()
            if ln and not ln.startswith((" ", "\t")) and "-" in ln.split()[0]]


def main() -> int:
    d = catalog_dir()
    fails, ran = [], [0]

    def check(name, cond, detail=""):
        ran[0] += 1
        print(f"  [{'ok ' if cond else 'FAIL'}] {name}" + ("" if cond else f"  {detail}"))
        if not cond:
            fails.append(name)

    # Returns both entries, alpha first. The rerank is then a visible inversion.
    query = "the first request is slow"

    # 1. Off by default. A flag that costs money must not fire because a test
    #    happened to have a key in the environment.
    with Gateway(lambda b, n: (200, answers({"lesson_0": 0.9, "lesson_1": 0.1}))) as g:
        rc, out = run(d, query, url=g.url)
        check("no call without --rerank", g.requests == [], f"{len(g.requests)} call(s)")
        check("plain query still works", rc == 0 and "alpha-cache" in out, f"rc={rc}")

    # 2. The judge reorders. BM25 puts alpha first for this query; scoring beta
    #    higher must invert the printed order.
    def invert(body, n):
        ids = [l["id"] for l in body["state"]["lessons"]]
        texts = {l["id"]: l["text"] for l in body["state"]["lessons"]}
        return 200, answers({i: (0.9 if "beta" in texts[i] else 0.1) for i in ids})

    with Gateway(invert) as g:
        rc, out = run(d, query, "--rerank", url=g.url)
        got = order(out)
        check("judged order beats search order",
              got and got[0].startswith("beta-retry"), f"order={got}")
        check("one call for the whole pool", len(g.requests) == 1, f"{len(g.requests)} call(s)")
        check("bearer token is sent",
              g.requests[0]["auth"] == "Bearer test-key", g.requests[0]["auth"][:12])
        sent = g.requests[0]["body"]
        check("one question per lesson",
              len(sent["questions"]) == len(sent["state"]["lessons"]) == 2,
              f"{len(sent['questions'])} q / {len(sent['state']['lessons'])} lessons")
        check("every question asks for a noul",
              all(q["type"] == "noul" for q in sent["questions"].values()))
        check("the query is the question asked",
              sent["state"]["question"] == query, sent["state"].get("question", "")[:40])
        check("judged score is shown", "judged 0.9" in out, out[:200])

    # 3. Ties keep the search order rather than shuffling it.
    with Gateway(lambda b, n: (200, answers(
            {l["id"]: 0.5 for l in b["state"]["lessons"]}))) as g:
        rc, plain = run(d, query, url=g.url)
        rc, tied = run(d, query, "--rerank", url=g.url)
        check("equal scores preserve search order", order(plain) == order(tied),
              f"{order(plain)} != {order(tied)}")

    # 4. No key: the query still answers, in search order, and says why.
    rc, out = run(d, query, "--rerank", url="http://127.0.0.1:1/", key=None)
    check("missing key does not fail the query", rc == 0 and "alpha-cache" in out, f"rc={rc}")
    check("missing key is explained", "AI_GATEWAY_API_KEY is not set" in out, out[:160])

    # 5. A 429 is retried, then gives up without failing the query. The measured
    #    rate wall was real, and a judge that dies on the first 429 is useless.
    calls = {"n": 0}

    def always_busy(body, n):
        calls["n"] = n
        return 429, {"error": "rate_limit_exceeded"}

    with Gateway(always_busy) as g:
        rc, out = run(d, query, "--rerank", url=g.url)
        check("429 is retried three times", calls["n"] == 3, f"{calls['n']} attempt(s)")
        check("429 falls back to search order", rc == 0 and "alpha-cache" in out, f"rc={rc}")
        check("429 is reported", "not reranked" in out and "429" in out, out[:200])

    # 6. A 400 is NOT retried — a rejected request will be rejected again.
    hits = {"n": 0}

    def bad_request(body, n):
        hits["n"] = n
        return 400, {"error": "bad request"}

    with Gateway(bad_request) as g:
        rc, out = run(d, query, "--rerank", url=g.url)
        check("400 is not retried", hits["n"] == 1, f"{hits['n']} attempt(s)")

    # 7. A missing answer must never read as 0.0. Scoring one lesson and
    #    silently calling the other irrelevant is the failure that would quietly
    #    bury a correct result.
    with Gateway(lambda b, n: (200, answers({"lesson_0": 0.8}))) as g:
        rc, out = run(d, query, "--rerank", url=g.url)
        check("a partial answer is refused, not defaulted",
              rc == 0 and "not reranked" in out and "no usable answer" in out, out[:200])

    # 8. The log carries the judged score only when there is one.
    plain_log, judged_log = d / "plain.jsonl", d / "judged.jsonl"
    with Gateway(lambda b, n: (200, answers(
            {l["id"]: 0.4 for l in b["state"]["lessons"]}))) as g:
        run(d, query, url=g.url, log=plain_log)
        run(d, query, "--rerank", url=g.url, log=judged_log)
    plain_rows = [json.loads(l) for l in plain_log.read_text().splitlines()]
    judged_rows = [json.loads(l) for l in judged_log.read_text().splitlines()]
    check("un-reranked rows keep their exact shape",
          plain_rows and all("jev" not in r for r in plain_rows))
    check("reranked rows carry the judged score",
          judged_rows and all(r.get("jev") == 0.4 for r in judged_rows),
          str(judged_rows[:1]))
    check("the search score is still logged",
          all(isinstance(r.get("score"), (int, float)) for r in judged_rows))

    # 9. The env var turns it on for a whole session.
    with Gateway(invert) as g:
        rc, out = run(d, query, url=g.url, extra={"RECALL_RERANK": "1"})
        check("RECALL_RERANK=1 enables it", len(g.requests) == 1, f"{len(g.requests)} call(s)")
    with Gateway(invert) as g:
        rc, out = run(d, query, url=g.url, extra={"RECALL_RERANK": "0"})
        check("RECALL_RERANK=0 leaves it off", g.requests == [], f"{len(g.requests)} call(s)")

    # 10. --json carries the judged score too, so a caller reading the machine
    #     output sees the same ordering evidence a reader does.
    with Gateway(invert) as g:
        rc, out = run(d, query, "--rerank", "--json", url=g.url)
        rows = json.loads(out)
        check("--json reports the judged score",
              rows and "jev" in rows[0] and rows[0]["id"].startswith("beta-retry"),
              str(rows[:1])[:160])

    print()
    if fails:
        print(f"FAIL — {len(fails)}/{ran[0]} checks red: {', '.join(fails)}")
        return 1
    print(f"PASS — {ran[0]}/{ran[0]} checks green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
