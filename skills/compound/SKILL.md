---
name: compound
description: |
  Use when turning a finished session into a reusable catalog entry, either
  interactively or as the unattended worker the scheduled drain launches.
  Use it for verified fixes, misleading errors and their true cause, tool and
  configuration facts, and corrections a person made more than once.
  Do not use it for ordinary summaries, or for anything the session did not
  actually verify.
version: 1.0.0
allowed-tools:
  - Read
  - Write
  - Edit
  - Grep
  - Glob
  - Bash
---

# /compound

You are the write half of the loop. `recall` retrieves; you are what puts
things there worth retrieving.

Your job is to read one finished session and leave behind entries that will
save a future agent the hour this session spent. Most sessions yield zero or
one. That is the normal outcome, not a failure.

The drain launches you unattended as:

```
<agent> -p "/compound <session-id>"
```

`<session-id>` names a transcript under `$RECALL_HOST_DIR/projects/*/`. When
invoked interactively with no argument, use the current session.

---

## Step 0 — Search before you write

**Always, before writing anything:**

```bash
python3 "$RECALL_SKILL_DIR/recall.py" "<your candidate in plain words>"
```

A close match means you add a recurrence to the entry that already exists. It
does not mean you write a second entry that says the same thing differently.

When you add a recurrence, add a dated key beside the counter:

```yaml
recurrences: 3
recurrence_2026_09_14: "the shape test could not express a multi-word command"
```

A bare counter cannot be placed on a timeline. Without a date nobody can answer
the only question that matters — *was the lesson shown before the failure
repeated?* — and those two cases need opposite fixes. A corpus of undated
counters measures nothing.

Automatic injection cannot show you every entry: it matches only code-shaped
tokens, so entries written in prose are invisible to it. Run the search.

---

## Step 1 — Read the session

Read the transcript. You are looking for the moment something stopped working
and the moment it started working again, and for what was true in between.

A candidate is one of:

- a fix, a workaround, or a recovery **that this session verified**
- a misleading error, paired with its true cause
- a fact about a tool, a configuration, a constraint, or an architecture
- a repeatable method whose benefit was observed, not assumed
- a decision or a reversal, with the reason
- a correction a person made twice

A repetition from a person is the strongest candidate there is. These mark one:
"again", "as I said", "I already told you", "we agreed", "you keep doing",
or the same point restated in different words.

---

## Step 2 — Apply the bar

Four questions. An entry needs all four.

| | |
|---|---|
| **Reusable** | Does it help a session that is not this one? |
| **Non-trivial** | Did it take discovery, or could the docs have said it? |
| **Specific** | Can you state the exact trigger and the exact fix? |
| **Verified** | Did the fix actually run here — not "should work"? |

Discard anything that fails one. A corpus of plausible-sounding entries is
worse than a small corpus, because it teaches the reader to stop trusting hits.

---

## Step 3 — Write the entry

Two catalogs live in `$RECALL_CATALOG_DIR`:

- `FAILURE_MODES.yaml` — code failed. A bug, a config, a runtime behaviour.
- `PROCESS_FAILURES.yaml` — the work failed. An agent or a person did the
  wrong thing in the right code.

The distinction is not cosmetic. They get retrieved at different moments: a
failure mode while writing code, a process failure while deciding what to do.

Append one list item:

```yaml
- id: a-short-sentence-that-states-the-lesson-in-kebab-case
  failure_class: one_token_naming_the_shape
  trigger: |
    What actually happened, concretely enough that a reader who was not here
    can recognise it. Name the real values, paths, and numbers.
  affected_pattern: |
    The general situation this recurs in — wider than the instance.
  fix_pattern: |
    What to do instead. Written so it can be followed, not admired.
  probe_when:
    - 'a sentence describing the moment, carrying `a-code-shaped-token`'
    - '`some_command --flag` appears in the work'
  probe_class: judgment           # static | runtime | judgment — these three only
  recurrences: 0
```

`id` and `probe_when` are required. Everything else improves retrieval.

### probe_when decides whether the entry can ever fire

The indexer reads **only backticked tokens** from `probe_when`. An item written
in plain prose is invisible to automatic injection — the entry sits in the
catalog, counted as covered, and never appears.

So every entry should carry at least one distinctive backticked token: a file
path, an error string, a flag, a symbol, a command.

Two rules learned the hard way:

- **A bare command is as specific as punctuation.** `git status` has no capital,
  digit, or symbol, so a naive shape test scores it as ordinary English and
  drops it. Multi-word commands are strong tokens — treat them as such.
- **Avoid single words that are also ordinary English.** `go`, `open`, `find`,
  `make`, `node` fire on "go look at the design". One noisy entry costs more
  than one missing entry, because it teaches the reader to ignore the channel.

If a lesson genuinely has no code-shaped token, write it anyway in prose.
`recall` reaches the whole corpus; only auto-injection is limited.

### probe_class has exactly three values

| value | meaning |
|---|---|
| `static` | a check on the code or the diff could have caught it |
| `runtime` | it had to actually run before anyone could see it |
| `judgment` | a person or agent had to notice; no mechanical trigger exists |

**Use these three and nothing else.** The corpus this was extracted from left the
equivalent field as free text, and it grew to 129 distinct values across 680
entries — 80 of them used exactly once. At that point the field cannot be
grouped, counted, or filtered, and it cannot be repaired afterwards either: a
classifier trained on those labels scores 57% against a 43% majority-class
baseline, so backfilling it means writing wrong values that look like data.

A vocabulary is cheap to constrain on the way in and impossible to recover on
the way out.

---

## Step 4 — Verify the write, then commit

Never report a capture you did not confirm landed.

```bash
python3 -c "import yaml,sys; d=yaml.safe_load(open('$RECALL_CATALOG_DIR/PROCESS_FAILURES.yaml')); print(len(d), d[-1]['id'])"
python3 "$RECALL_SKILL_DIR/recall.py" --id <the-id-you-wrote>
```

Then rebuild the index so the entry can fire, and commit if the catalogs are
under version control:

```bash
python3 "$RECALL_SKILL_DIR/build_probe_index.py"
```

Stage only the catalog files. Never stage the whole tree: the catalog directory
is frequently shared with other work, and a broad `git add` sweeps in changes
that are not yours. A pure append shows as insertions with **zero deletions** —
if the diffstat shows deletions, stop and look at what you picked up.

If a write fails, say so. Do not report a capture for a failed write.

---

## What not to do

- **Do not write an entry for something the session did not verify.** A
  plausible fix that was never run is a guess with a citation.
- **Do not create a second entry for a lesson that exists.** Recurrences are
  the more valuable signal: they say the lesson was written down and repeated
  anyway, which is the only measurement of whether any of this works.
- **Do not summarise the session.** The transcript already exists. Write only
  what a future agent could not reconstruct from the code and the git log.
- **Do not soften the entry.** "Be careful with X" is not a fix pattern. Name
  the condition and the action.
- **Do not write secrets, credentials, tokens, or private content into a
  catalog.** Catalogs are frequently committed. Reference a path; never a value.

---

## When nothing qualifies

Write nothing and exit cleanly. Most sessions yield nothing, and an empty
result is a correct result.

The drain distinguishes "did the work and found nothing" from "failed to run"
by exit code, not by whether you produced output. Exit 0 having written
nothing is a success.
