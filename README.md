# Recall

**Your AI coding agent forgets everything the moment a session ends. Recall fixes that.**

You already explained this once. The setting that ate a day last month. The reason
you stopped doing it the other way. The thing that finally worked at 1am. Your agent
worked it out with you, you moved on, and when the conversation closed it was gone.

So next week you explain it again.

Recall catches what a session actually taught, keeps it on your own machine, and
puts it back in front of your agent the next time it matters. It works with
**Claude Code** and **Codex**, in the terminal and in the desktop app.

Paste this into a Claude Code session and you are done:

> Install the Recall plugin. Run `claude plugin marketplace add ok-wow/recall`,
> then `claude plugin install recall@ok-wow`, and show me what each one printed.

(The repo is private to our team for now. [Getting access](#getting-access) is two
commands, once.)

---

## What you get

**It learns.** When a session ends, Recall reads it and writes down what it taught.
Not a summary of what happened. The thing that finally worked, the decision and what
you turned down to make it, the approach that looked right and wasn't.

**It speaks up.** The next time that lesson matters, your agent already has it. Some
lessons carry something exact, like a file name or an error message. When that exact
thing shows up again, Recall says so on its own. You do not have to remember to ask.

**You can ask.** The rest are judgment calls. Why you dropped that approach. How this
client likes to be handled. Ask in plain words and it finds them anyway.

**It finds what you decided.** If you keep decision docs, Recall reads the decision
log inside each one, so "what did we decide about the tables" gets the decision, not
the document it is buried in. In the memory Recall grew up in, that is 1,515
decisions across 166 docs that a search used to miss.

**It remembers what you put off.** Every session ends with a few things you decided
not to do yet. Recall keeps a parking lot for them, with a "do next" list that holds
five items and refuses a sixth. More on that [below](#work-you-said-you-would-do-later).

**It adds up.** Every session starts further along than the last one. Your work stops
resetting.

## The part most memory tools skip

Let me be blunt about this one, because it is the reason I built it.

**Most memory tools can tell you how often they fired. They cannot tell you whether
it helped.** So you get a number that goes up and no idea if anything got better.

Recall keeps score, and it will tell you bad news. I ran it on my own memory today:

- **442 times**, a lesson I had already written down came true again.
- **244 of those** it could measure. Of them, **213** had been put in front of
  someone first. The lesson got read, and it did not change what happened.
- **31** were sitting right there and never came up.

Those two numbers look the same on a dashboard and are opposite problems in real
life. The 31 need better search. The 213 need better writing, and more search will
not help them at all. A tool that reports one number cannot tell you which one you
have.

I would rather know that than keep a nicer number.

## It stays on your machine

**Nothing to sign up for.** No account, no cloud, no dashboard. Your memory is a
folder on your own disk, `~/.recall`.

**Nothing to run.** It works inside your agent, at the start and end of each session.
Working out what a session taught takes one model call, and it goes to the same agent
you were already using. Your memory itself never travels anywhere.

**Nothing borrowed.** It starts empty. What fills it is your work and only your work,
so it reads back in your own words from the very first lesson.

**Never a secret.** It records where a password lives, never the password.

## What it keeps, and what it throws away

| It keeps | |
|---|---|
| **Things that broke** | The misleading error and its real cause. The setting that only bites under load. |
| **Ways of working that went wrong** | Nothing was broken, the approach was. Checked the wrong thing and called it done. |
| **Decisions** | What you picked, what you turned down, and why. Reversals count double. |
| **Work you put off** | What you said you would do later, so later does not turn into never. |

A lesson has to clear four bars to survive: it helps a future session, not just this
one; it took real discovery, not a glance at the docs; it names an exact trigger and
an exact fix; and that fix actually ran.

Most sessions produce nothing that clears all four. That is the expected result, and
it is why the memory stays worth reading.

## It will tell you when it has nothing

Ask most memory tools about something you have never hit and they hand you their
closest match anyway. It looks like an answer. You read it, it does not help, and you
trust the thing a little less every time.

Recall says so instead:

```
  no lesson here answers that
```

It works that out by comparing, not by guessing a cutoff. Every question carries one
extra candidate along: a few lines written to be useless, shaped like a lesson, saying
nothing. A real answer has to beat that by a clear margin. On 27 test questions, the
16 it could answer beat the useless text by a mile, and the 11 it could not did not
beat it at all.

## Work you said you would do later

Every session ends with a few things you decided not to do yet. They used to go into
a handoff note, and nothing ever put that note in front of anyone again. Later quietly
turned into never, and you could not tell which items had.

So Recall keeps a parking lot. Any session can park an item, and a plain question
finds it again, right next to the lessons. When an item matches what you are working
on, it shows up on its own.

```bash
python3 ~/recall/scripts/park.py add --title "Retry the Slack sync on rate limits" --tier 2
python3 ~/recall/scripts/park.py list          # do next, then soon, then someday, then the inbox
python3 ~/recall/scripts/park.py set <id> --tier 1
python3 ~/recall/scripts/park.py done <id>
python3 ~/recall/scripts/recall.py "what did we park about slack"
```

**The "do next" list holds five items. Not six.** A sixth is refused, the five are
shown, and there is no way to force it. You demote one first. That is the point: a
do-next list where everything is urgent is a list you stop reading.

**Parking the same thing twice is caught.** An item that says what an open one already
says is refused, and you are shown the open one. Ten sessions noticing the same problem
should update one item, not leave ten.

## Getting access

The repo is private to the okWOW team for now, so your GitHub account needs access to
it. Ask a teammate to add you, then sign in to GitHub once from a terminal and let git
use that sign-in:

```bash
gh auth login
```

```bash
gh auth setup-git
```

If you do not have `gh`, install it from [cli.github.com](https://cli.github.com/).

## Install

Pick one. One per machine is enough.

### Claude Code, by prompt

Open a Claude Code session, in the terminal or in the desktop app, and paste:

> Install the Recall plugin. Run `claude plugin marketplace add ok-wow/recall`,
> then `claude plugin install recall@ok-wow`, and show me what each one printed.

Start a new session and it is on. The first session makes `~/.recall` for you. The
desktop app and the terminal share the same setup, so one install covers both.

### Claude Code, by command

```bash
claude plugin marketplace add ok-wow/recall
```

```bash
claude plugin install recall@ok-wow
```

To get a newer Recall later, then restart the session:

```bash
claude plugin update recall@ok-wow
```

### Codex

```bash
git clone https://github.com/ok-wow/recall.git ~/recall && cd ~/recall && ./install.sh --host codex
```

### Claude Code, from a clone

```bash
git clone https://github.com/ok-wow/recall.git ~/recall && cd ~/recall && ./install.sh
```

The installer prints a plan, asks once, and backs up every file it touches. There is
an uninstaller and it puts everything back.

Use the plugin or the clone, not both. With both, every step runs twice, and Recall
tells you so at the start of a session.

### Where it does not run

Cloud and web sessions do not load plugins, so Recall is off there. It runs where your
own `~/.claude` or `~/.codex` folder is.

## Ask it something

From a clone, in plain words:

```bash
python3 ~/recall/scripts/recall.py "how do we handle a renewal that slipped"
```

The plugin keeps its scripts inside its own folder. To run them by hand, keep a clone
too. A clone changes nothing on your machine until you run `install.sh`.

## A private backup of your own

What Recall stores about you lives in `~/.recall`. It has no history and no backup,
and it does not belong in a team repo. This gives it both, in a private repo under
your own GitHub account.

```bash
python3 ~/recall/scripts/personal_repo.py plan          # shows what it would do, changes nothing
python3 ~/recall/scripts/personal_repo.py init --apply  # makes the folder a repo and commits it
python3 ~/recall/scripts/personal_repo.py status        # any time
```

It never creates a GitHub repo and never pushes. It prints those two commands and you
run them. Passwords, logs and caches stay out of every commit, and a file that looks
like a key stops the commit. It refuses a repo that is public or that belongs to an
organization, because an organization's owners can read every repo in it.

## Honestly

Recall cannot prove it made anything better. It can show you that a lesson appeared
before a problem did not repeat, and that is encouraging rather than proof. The
problem might simply not have come back. Settling it properly would mean running with
the memory switched off for a while as a comparison, and it does not do that.

It tells you what it measured and where the measuring stops.

---

**Technical documentation:** [docs/reference.md](docs/reference.md) covers every
command, every setting, how each measurement is taken, and the reason behind each
guard in the code.

MIT licensed.
