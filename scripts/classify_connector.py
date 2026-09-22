#!/usr/bin/env python3
"""classify_connector — ask Jev the configured questions before a record enters.

The collector is an agent session, so "use judgment about what to index" is a
prose instruction given to a model, which is the weakest control this project
knows. This makes the judgment a scored, configured, testable step: the axes
live in connector_questions.yaml, every record is scored on each one, and the
verdict falls out of the roles rather than out of a summary.

    cat records.json | classify_connector.py            # keep/review/drop + scores
    cat records.json | classify_connector.py --keep     # only the keeps, as JSON

Three verdicts, not two. `review` is not indecision -- a score near a floor is
a judgment call, and collapsing it into keep or drop throws away the one signal
that says which records a person should actually look at.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
CONFIG = Path(os.environ.get("RECALL_CONNECTOR_QUESTIONS")
              or HERE / "connector_questions.yaml")


def load_axes(path: Path = None) -> dict:
    import yaml
    data = yaml.safe_load((path or CONFIG).read_text()) or {}
    axes = data.get("axes") or []
    if not axes:
        raise SystemExit(f"classify_connector: no axes in {path or CONFIG}")
    for a in axes:
        if a.get("role") not in ("require", "any_of", "exclude"):
            raise SystemExit(f"classify_connector: axis {a.get('id')!r} has no valid role")
        if a["role"] == "exclude" and "max" not in a:
            raise SystemExit(f"classify_connector: exclude axis {a['id']!r} needs `max`")
        if a["role"] != "exclude" and "min" not in a:
            raise SystemExit(f"classify_connector: axis {a['id']!r} needs `min`")
    return data


def as_text(rec: dict) -> str:
    """What the judge sees. Deliberately the same fields the index will hold --
    scoring a richer version than the one being stored would measure a record
    that never exists."""
    bits = [str(rec.get("title") or ""), str(rec.get("gist") or "")]
    where = str(rec.get("where") or "")
    if where:
        bits.append(f"({where})")
    return " ".join(b for b in bits if b).strip()


def verdict(scores: dict, data: dict) -> tuple[str, str]:
    """Returns (keep|review|drop, the reason, in words a person can check)."""
    margin = float(data.get("review_margin") or 0.05)
    near = []
    for a in data["axes"]:
        s = scores.get(a["id"], 0.0)
        if a["role"] == "exclude":
            if s >= a["max"]:
                return "drop", f"{a['id']} {s:.2f} >= {a['max']}"
            if s >= a["max"] - margin:
                near.append(f"{a['id']} {s:.2f} near {a['max']}")
    for a in data["axes"]:
        if a["role"] == "require":
            s = scores.get(a["id"], 0.0)
            if s < a["min"]:
                return "drop", f"{a['id']} {s:.2f} < {a['min']}"
            if s < a["min"] + margin:
                near.append(f"{a['id']} {s:.2f} near {a['min']}")
    any_of = [a for a in data["axes"] if a["role"] == "any_of"]
    if any_of:
        cleared = [a for a in any_of if scores.get(a["id"], 0.0) >= a["min"]]
        if not cleared:
            best = max(any_of, key=lambda a: scores.get(a["id"], 0.0))
            return "drop", (f"no any_of axis cleared "
                            f"(best {best['id']} {scores.get(best['id'], 0.0):.2f} "
                            f"< {best['min']})")
        if all(scores[a["id"]] < a["min"] + margin for a in cleared):
            near.append(", ".join(f"{a['id']} {scores[a['id']]:.2f} near {a['min']}"
                                  for a in cleared))
    if near:
        return "review", "; ".join(near)
    return "keep", "clears every axis"


def classify(records: list[dict], data: dict = None, scorer=None) -> list[dict]:
    """One Jev call PER AXIS, all records together -- never one call per record.

    Scores from a single call are comparable to each other, which is the whole
    basis of the floors below; scoring records one at a time would make every
    threshold a comparison across unrelated calls.
    """
    data = data or load_axes()
    if scorer is None:
        import jev
        scorer = jev.score
    texts = [as_text(r) for r in records]
    by_axis = {}
    for a in data["axes"]:
        q = " ".join(str(a["question"]).split())
        by_axis[a["id"]] = scorer(q, texts)
    out = []
    for i, r in enumerate(records):
        scores = {aid: float(v[i]) for aid, v in by_axis.items()}
        v, why = verdict(scores, data)
        out.append({"record": r, "scores": scores, "verdict": v, "why": why})
    return out


def _read_stdin() -> list[dict]:
    raw = sys.stdin.read().strip()
    if not raw:
        raise SystemExit("classify_connector: nothing on stdin")
    try:
        d = json.loads(raw)
        return d if isinstance(d, list) else [d]
    except json.JSONDecodeError:
        return [json.loads(l) for l in raw.splitlines() if l.strip()]


def main() -> int:
    if "--key-from" in sys.argv:
        # The key lives in one .env and nowhere else; read it, never echo it.
        p = Path(sys.argv[sys.argv.index("--key-from") + 1]).expanduser()
        for line in p.read_text().splitlines():
            m = re.match(r"\s*AI_GATEWAY_API_KEY\s*=\s*(.+)", line)
            if m:
                os.environ["AI_GATEWAY_API_KEY"] = m.group(1).strip().strip("'\"")
                break
    records = _read_stdin()
    data = load_axes()
    rows = classify(records, data)
    if "--keep" in sys.argv:
        print(json.dumps([r["record"] for r in rows if r["verdict"] == "keep"],
                         ensure_ascii=False))
        return 0
    ids = [a["id"] for a in data["axes"]]
    print("  " + "".join(f"{i[:7]:>8}" for i in ids) + "  verdict  item")
    for r in sorted(rows, key=lambda r: ("keep review drop".split().index(r["verdict"]),
                                         -sum(r["scores"].values()))):
        cells = "".join(f"{r['scores'][i]:8.2f}" for i in ids)
        print(f"  {cells}  {r['verdict']:<7}  {as_text(r['record'])[:60]}")
        if r["verdict"] != "keep":
            print(f"  {'':>{8*len(ids)}}           ^ {r['why']}")
    n = {v: sum(1 for r in rows if r["verdict"] == v) for v in ("keep", "review", "drop")}
    print(f"\n  {n['keep']} keep · {n['review']} review · {n['drop']} drop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
