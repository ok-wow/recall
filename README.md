# Recall

**Your agent forgets everything the moment a session ends. This fixes that.**

You already explained this. The gotcha that ate a day last month. The reason you
stopped doing it the other way. The thing that finally worked at 1am. Your agent
figured it out with you, you moved on, and when the session closed it all went.

So next week you explain it again.

Recall catches what a session actually taught, and puts it back in front of your
agent the next time it matters.

```bash
git clone https://github.com/ok-wow/recall.git ~/recall && cd ~/recall && ./install.sh
```

---

## Three things happen

**Learn.** When a session ends, Recall works out what it taught. Not a summary of
what happened - the thing that finally worked, the decision and what you turned
down to make it, the approach that looked right and wasn't.

**Recall.** The next time that lesson matters, your agent already has it. You
don't go digging for it. It is already there.

**Compound.** Every session starts further along than the last one. Your work
stops resetting and starts adding up. That is the whole point, and it is the
thing a tool you use every day either does or doesn't.

## The part most memory tools skip

Let me be blunt about this one, because it is the reason I built it.

**Most memory tools can tell you how often they fired. They cannot tell you
whether it helped.** So you get a number that goes up and no idea if anything
got better.

Recall keeps score, and it will tell you bad news. Run it on my own memory today
and here is what comes back:

- **155 times** a lesson I had already written down came true again.
- **125 of those**, the lesson had been put in front of someone first. It got
  read and it did not change what happened.
- **30 times**, it was sitting right there and never came up.

Those two numbers are the same failure on a dashboard and opposite problems in
real life. The 30 need better search. The 125 need better writing - more search
will not help them at all. A tool that reports one number cannot tell you which
one you have.

Then it goes one level further and checks itself. Some of those 125 had been
delivered with their last sentence cut off, because the part that tells you
what to do was too long to print. Advice that stops mid-sentence looks delivered
and isn't.

I would rather know that than keep a nicer number.

## Two ways to remember, and you need both

**It tells you.** Some lessons carry something exact - a file path, an error
message, a client name. When that exact thing turns up again, Recall speaks up
on its own, without being asked.

**You ask it.** The rest are judgment calls. Why you dropped that approach. How
this client likes to be handled. Those never carry anything exact to match on,
so you ask in plain words and it finds them anyway.

Skip the second half and you have a tool that files your best thinking perfectly
and never shows it to you again. In the memory Recall came from, judgment calls
were **43%** of everything worth keeping.

## It stays on your machine

**Nothing to sign up for.** No account, no cloud, no telemetry, no dashboard.
Your memory is a file on your own disk.

**Nothing to provision.** No database, no service, no index to rebuild. It runs
inside a hook and a scheduled job, because those are the only places it needs to
run.

**Nothing borrowed.** It starts empty. What fills it is your work and only your
work, so it reads back in your own words from the very first lesson.

Working out what a session taught takes one model call, and it goes to the same
agent you were already using. Nowhere new. Your memory itself never travels.

## What it keeps, and what it throws away

| It keeps | |
|---|---|
| **Things that broke** | The misleading error and its real cause. The setting that only bites under load. The version that quietly changed behaviour. |
| **Ways of working that went wrong** | Nothing was broken - the approach was. Checked the wrong case and called it done. Trusted a stale copy. Shipped a claim nothing tested. |
| **Decisions** | What you picked, what you turned down, and why. Reversals count double. |

A lesson has to clear four bars to survive: it helps some session that is not
this one, it took real discovery rather than a glance at the docs, it names an
exact trigger and an exact fix, and that fix actually ran.

Most sessions produce nothing that clears all four. That is the expected result,
and it is why the memory stays worth reading.

It will never keep a secret. It records where a credential lives, never the
credential.

## It will tell you when it has nothing

Ask most memory tools about something your team has never hit and they hand you
their closest row anyway. It looks like an answer. You read it, it doesn't help,
and you trust the thing a little less every time.

Recall says so instead:

```
  no lesson here answers that
```

It works that out by comparing, not by setting a cutoff. Every question it
judges carries one extra candidate along - a few lines written to be useless,
shaped like a lesson, saying nothing about anything. A real answer has to beat
that by a clear margin.

I checked it on 27 questions. The 16 it could answer beat the useless text by a
mile. The 11 it couldn't didn't beat it at all. There was no overlap between
them, which is what made the rule safe to ship.

You can always ask to see the results anyway.

## Try it

It works with **Claude Code** and **Codex**, in the terminal and in the desktop
app. It tells you everything it is about to do before it does any of it.

The repo is private to the okWOW org for now, so your GitHub account needs
access to it. Sign in once, then let git use that sign-in:

```bash
gh auth login
```

```bash
gh auth setup-git
```

Pick one of the ways below. One per machine is enough.

### Claude Code, by command

```bash
claude plugin marketplace add ok-wow/recall
```

```bash
claude plugin install recall@ok-wow
```

Start a new session and it is on. There is no install step after that: the
first session makes `~/.recall` and its empty catalogs.

To get a newer Recall later, then restart the session:

```bash
claude plugin update recall@ok-wow
```

### Claude Code, by prompt

Paste this into a session, in the terminal or in the desktop app:

> Install the Recall plugin. Run `claude plugin marketplace add ok-wow/recall`,
> then `claude plugin install recall@ok-wow`, and show me what each one printed.

The desktop app and the terminal read the same `~/.claude` folder, so one
install covers both.

### Codex

```bash
git clone https://github.com/ok-wow/recall.git ~/recall && cd ~/recall && ./install.sh --host codex
```

Codex gets the same five hooks, and the skill is linked where Codex reads
skills, in `~/.agents/skills`.

### Claude Code, from a clone

```bash
git clone https://github.com/ok-wow/recall.git ~/recall && cd ~/recall && ./install.sh
```

The installer prints a plan, asks once, and backs up every file it edits.
There is an uninstaller and it puts everything back.

Use the plugin or the clone, not both. Each one registers the hooks, so with
both every hook runs twice. Recall says so at the start of a session and names
the command that removes the extra copy.

### Where it does not run

Cloud and web sessions do not load plugins, so Recall is off there. It runs
where your own `~/.claude` or `~/.codex` folder is.

### Ask it something

From a clone, in plain words:

```bash
python3 ~/recall/scripts/recall.py "how do we handle a renewal that slipped"
```

The plugin keeps its scripts inside its own folder. To run them by hand, keep a
clone too. A clone changes nothing on your machine until you run `install.sh`.

### A private repo of your own

What Recall stores about you lives in `~/.recall`: the catalogs, the parking
lot, the record of what was shown. It has no history and no backup, and it does
not belong in a team repo. This gives it both, in a private repo under your own
account.

See the plan first. It changes nothing:

```bash
python3 ~/recall/scripts/personal_repo.py plan
```

Make the store a git repo and commit it:

```bash
python3 ~/recall/scripts/personal_repo.py init --apply
```

Check it at any time:

```bash
python3 ~/recall/scripts/personal_repo.py status
```

It never creates a GitHub repo and never pushes. It prints those two commands
and you run them. Credentials, logs and caches stay out of every commit, and a
file that looks like a key stops the commit. It refuses a remote that is public
or that belongs to an organization, because an organization's owners can read
every repo in it.

## Honestly

Recall cannot prove it made anything better. It can show you that a lesson
appeared before a problem did not repeat, and that is encouraging rather than
proof - the problem might simply not have come back. Settling it properly would
take running with the memory switched off for a while as a comparison, and it
does not do that.

It tells you what it measured and where the measuring stops.

---

**Technical documentation:** [docs/reference.md](docs/reference.md) - every
command, every setting, how each measurement is taken, and the reason behind
each guard in the code.

MIT licensed.
