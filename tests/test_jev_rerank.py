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


# recall.py rides a deliberately useless candidate along in the same call and
# abstains when nothing beats it. Matched by text rather than by importing the
# constant: recall resolves its catalog dir at import time, and these cases run
# it as a subprocess on purpose.
CONTROL_MARK = "open that file and look at what it contains"


def policy(real, control: float = 0.05):
    """Score each real lesson with `real(text)`, and the control with `control`."""
    def answer(body, n):
        return 200, answers({l["id"]: (control if CONTROL_MARK in l["text"]
                                       else real(l["text"]))
                             for l in body["state"]["lessons"]})
    return answer


def has_control(body) -> bool:
    return any(CONTROL_MARK in l["text"] for l in body["state"]["lessons"])


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
           # Pin EVERY corpus path. "The last unpinned one" was wrong twice:
           # each new source defaults to a real location under $HOME, and an
           # unpinned one does not fail loudly -- it quietly enlarges the
           # fixture. RECALL_SPECS_INDEX went in on 2026-09-21 and put 159 real
           # specs into 5-entry corpora, reddening 7 checks across two suites.
           # The "--stats counts every entry" check is the structural guard:
           # it compares the fixture's own entry count against what recall
           # reports, so it reddens the moment any source leaks in.
           "RECALL_SKILLS_DIR": str(Path(__file__).resolve().parent / "fixtures" / "no-skills"),
           "RECALL_SPECS_INDEX": str(d / "nonexistent-specs.txt"),
           "RECALL_HUB_INDEX": str(d / "nonexistent-hub.json"),
           "RECALL_CONNECTOR_DIR": str(d / "nonexistent-connectors"),
           "RECALL_RECEIPT_DIR": str(d / "nonexistent-receipts"),
           "RECALL_ORPHAN_INDEX": str(d / "nonexistent-orphans.yaml"),
           "RECALL_LOT_DIR": str(d / "nonexistent-lot"),
           "RECALL_SPECS_DIR": str(d / "nonexistent-specs-dir"),
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
    invert = policy(lambda t: 0.9 if "beta" in t else 0.1)

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
              len(sent["questions"]) == len(sent["state"]["lessons"]) == 3,
              f"{len(sent['questions'])} q / {len(sent['state']['lessons'])} lessons")
        check("the control rides in the same call, not a second one",
              has_control(sent) and len(g.requests) == 1)
        check("the control is never shown as a result",
              "A project keeps some of its files" not in out, out[:200])
        check("every question asks for a noul",
              all(q["type"] == "noul" for q in sent["questions"].values()))
        check("the query is the question asked",
              sent["state"]["question"] == query, sent["state"].get("question", "")[:40])
        check("judged score is shown", "judged 0.9" in out, out[:200])

    # 3. Ties keep the search order rather than shuffling it.
    with Gateway(policy(lambda t: 0.5)) as g:
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
    with Gateway(policy(lambda t: 0.4)) as g:
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

    # 11. Abstention. Pull's other half already refuses to answer -- the push
    #     index carries three score floors -- while this side had none, so a
    #     nonsense question still came back with a confident top hit. The floor
    #     here is a comparison, because the absolute numbers are the part this
    #     project measured and does not trust.
    nothing_clears = policy(lambda t: 0.04)          # both real lessons under 0.05
    with Gateway(nothing_clears) as g:
        rc, out = run(d, query, "--rerank", url=g.url)
        check("nothing beats the control -> no results", rc == 0 and not order(out),
              f"order={order(out)}")
        check("abstention says so in words", "no lesson here answers that" in out, out[:200])
        check("abstention names the way out", "--no-abstain" in out, out[:200])

    # A tie with the control is not an answer either. A lesson that merely
    # matches something content-free has told the reader nothing.
    with Gateway(policy(lambda t: 0.05)) as g:
        rc, out = run(d, query, "--rerank", url=g.url)
        check("a tie with the control abstains", rc == 0 and not order(out),
              f"order={order(out)}")

    # The case the live measurement turned on: a lesson that BEATS the control
    # but only just. On a question the corpus cannot answer, every score
    # collapses to within about 0.03 of every other -- so "higher than the
    # control" alone would have shown junk on 6 of 11 nonsense questions.
    with Gateway(policy(lambda t: 0.12)) as g:      # control 0.05, gap 0.07
        rc, out = run(d, query, "--rerank", url=g.url)
        check("edging past the control is not an answer",
              rc == 0 and not order(out), f"order={order(out)}")

    with Gateway(policy(lambda t: 0.12)) as g:
        rc, out = run(d, query, "--rerank", url=g.url,
                      extra={"RECALL_ABSTAIN_MARGIN": "0.01"})
        check("the margin is tunable", rc == 0 and order(out), f"order={order(out)}")

    # 12. --no-abstain reads them anyway, and pays for no control slot.
    with Gateway(nothing_clears) as g:
        rc, out = run(d, query, "--rerank", "--no-abstain", url=g.url)
        check("--no-abstain shows the results", rc == 0 and order(out), f"order={order(out)}")
        check("--no-abstain sends no control",
              not has_control(g.requests[0]["body"])
              and len(g.requests[0]["body"]["state"]["lessons"]) == 2,
              str(len(g.requests[0]["body"]["state"]["lessons"])))

    # 13. An abstention logs nothing as surfaced -- because nothing was -- but
    #     leaves a countable row. A query answered with silence and a query
    #     nobody ran must not look the same to the coverage metric.
    abstain_log = d / "abstained.jsonl"
    with Gateway(nothing_clears) as g:
        run(d, query, "--rerank", url=g.url, log=abstain_log)
    rows = [json.loads(l) for l in abstain_log.read_text().splitlines()]
    check("an abstention surfaces no entry",
          all("entry" not in r for r in rows), str(rows[:2])[:160])
    check("an abstention is still recorded",
          len(rows) == 1 and rows[0]["event"] == "recall-abstain"
          and rows[0]["judged"] == 2, str(rows[:1])[:160])
    # ...and something reads that row. A gate whose only output goes somewhere
    # nobody looks cannot be checked by the person relying on it.
    with Gateway(invert) as g:
        run(d, query, "--rerank", url=g.url, log=abstain_log)
    rc, out = run(d, "--stats", "--json", log=abstain_log)
    st = json.loads(out)
    check("--stats counts abstentions against pulls",
          st.get("pulls_answered") == 1 and st.get("pulls_abstained") == 1
          and st.get("abstained_pct") == 50.0, str(st)[-120:])

    # 14. --json abstains to an empty list, not to a confident wrong answer.
    with Gateway(nothing_clears) as g:
        rc, out = run(d, query, "--rerank", "--json", url=g.url)
        check("--json abstains to []", rc == 0 and json.loads(out) == [], out[:160])

    # 15. Without --rerank there is no judge, so there is no abstention: the
    #     query keeps answering in search order exactly as it always did.
    with Gateway(nothing_clears) as g:
        rc, out = run(d, query, url=g.url)
        check("no judge, no abstention", rc == 0 and order(out), f"order={order(out)}")

    print()
    if fails:
        print(f"FAIL — {len(fails)}/{ran[0]} checks red: {', '.join(fails)}")
        return 1
    print(f"PASS — {ran[0]}/{ran[0]} checks green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
