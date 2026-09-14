# okWOW • Recall

A coding agent solves a problem, you close the session, and the solution is gone. Next
week it hits the same wall and works it out again from scratch. **okWOW • Recall** is a loop that
turns finished sessions into a searchable corpus of failure modes, and puts the relevant
ones back in front of the agent while it works.

It runs entirely on your machine. Nothing is sent anywhere.

## The shape of it

```
  session ends ──► is there real work here? ──► queue
                                                 │
                     (scheduled, unattended)     ▼
                 a fresh agent reads the session and writes down
                 what failed, why, and the fix ──► catalogs (YAML)
                                                     │
                        ┌────────────────────────────┴───────────────┐
                        ▼                                            ▼
                  PUSH: matched against your prompts           PULL: you ask
                  and injected automatically                   `recall "question"`
```

Two halves, and **you need both**. This is the part most such systems get wrong.

**Push** is automatic and must be precise. It fires without being asked, so a false
positive costs more than a miss — a channel that cries wolf gets ignored, and then the one
hit that mattered is invisible too. So push only matches distinctive, code-shaped literals:
`allow-same-origin`, `table=True`, `dark:bg-gray-800`.

**Pull** is on demand and must be complete. Precision can be looser because a person asked.
It does BM25 over the full text of every entry, including the ones push can never reach.

Without pull, every lesson that is *conceptual* rather than a literal string gets written
down perfectly and never surfaces again. In the corpus this was extracted from, that was
**43% of everything captured** — 592 of 1375 entries — and nobody noticed for months,
because a retrieval system that finds nothing looks exactly like a codebase with no
matching lessons.

## Install

```bash
git clone <this repo> ~/recall && cd ~/recall && ./install.sh
```

`install.sh` registers the hooks with your agent, creates `~/.recall`, and schedules the
drain. It prints everything it is about to do and asks first.

Requires Python 3.9+, PyYAML, and an agent CLI that supports session hooks.

## Use

```bash
# ask the corpus — plain language, no query syntax
recall "test passes on CI but fails locally"

# one entry in full, with the triggers that make it fire
recall --id test-runner-inherits-ambient-timezone

# what has repeated ANYWAY, despite being written down
recall --recurring

# corpus and retrieval health
recall --stats
```

Everything else is automatic. Sessions get captured when they end, distilled on a schedule,
and matched against your prompts while you work.

## Configuration

Every path resolves through an environment variable with a default. No absolute paths.

| Variable | Default | Holds |
|---|---|---|
| `RECALL_HOME` | `~/.recall` | all mutable state |
| `RECALL_CATALOG_DIR` | `$RECALL_HOME/catalogs` | the YAML catalogs |
| `RECALL_AGENT_BIN` | `claude` | CLI used for unattended distillation |
| `RECALL_HOST_DIR` | `~/.claude` | your agent's dir (transcripts, settings) |

## What it deliberately does not do

- **No cloud, no telemetry, no account.** The corpus is your sessions. It stays local.
- **No embedding model.** BM25 over full text, pure stdlib. It has to run inside a hook,
  a cron job, and a fresh clone with nothing installed.
- **No content ships.** This repo is the mechanism. The catalogs start empty and fill with
  your own failures.

## Honest limitations

- **Surfacing is not prevention.** Every measurement here proves the right entry appeared.
  None proves it changed an outcome. In the source corpus 10% of entries recurred *anyway*
  — written down, and repeated regardless. Treat the recurrence counter as the real
  scoreboard, not the fire count.
- **Push cannot reach prose.** An entry whose triggers are conceptual rather than literal
  will never auto-inject. That is why `recall` exists and why the agent is told to run it.
- **Distillation costs tokens.** The drain spawns a headless agent per session. It is
  bounded by a wall-clock budget and a per-session timeout, and it is idle-cheap, but it
  is not free.
- **Quality depends on your sessions.** A corpus distilled from sloppy work is a corpus of
  sloppy lessons.

## Why the comments are long

Most guards in this codebase exist because something specific broke. The comment beside a
guard says which failure it prevents, because a guard whose reason is forgotten gets
removed by the next person who finds it inconvenient. If you are reading a condition that
looks paranoid, the comment tells you what happened.

A few that shaped the design:

- The drain measured "did this get done?" by whether the worker deleted its own marker.
  A worker that did the job correctly and declined the bookkeeping was recorded as failed,
  retried, and quarantined. 60 sessions went missing into a directory nothing counted.
  Progress is now derived from what the dispatcher observes — exit code, kill status.
- The oversize gate parked large transcripts in a tier with no consumer. Because transcript
  size tracks session length, it was collecting the *longest working sessions* — the best
  material in the queue. A gate must hand overflow to a reduced path, not a dead end.
- The test suite drove the real hook, which logged to the real signal log. 45% of the
  retrieval evidence was fixtures, and the published baseline was wrong in both directions.
  Every durable write path now takes an env override and the suite asserts it did not move.

## Tests

```bash
python3 -m tests.run    # or: for t in tests/test_*.py; do python3 "$t"; done
```

The suites assert behaviour that matters rather than coverage: that a correct no-op is
distinguishable from a failure, that housekeeping runs on the idle path, that a test cannot
write to production state, and that an entry written in prose is still retrievable.

## License

MIT. See LICENSE.
