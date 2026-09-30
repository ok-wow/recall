# Recall by okWOW

**The memory your AI agent was missing.**

Recall is a small add-on for **Claude Code** and **Codex**. It remembers what your
work taught: what worked, what you decided, and what you put off. It runs on your
own machine, in the terminal or the desktop app, and it starts empty.

It is for anyone who works with a coding agent every day and is tired of explaining
the same things again. Founders, designers, operators, engineers. If you have ever
typed one of these, this is for you:

- "No, do it this way." For the second time.
- "We just solved this yesterday."
- "Why the fuck can you not remember what we talked about?"

## Get it

**Claude Code.** Paste this into a session, in the terminal or in the desktop app:

> Install the Recall plugin. Run `claude plugin marketplace add ok-wow/recall`,
> then `claude plugin install recall@ok-wow`, and show me what each one printed.

Or run the two commands yourself:

```bash
claude plugin marketplace add ok-wow/recall
```

```bash
claude plugin install recall@ok-wow
```

**Codex.** One line:

```bash
git clone https://github.com/ok-wow/recall.git ~/recall && cd ~/recall && ./install.sh --host codex
```

Start a new session and it is on.

---

## The problem

Your agent is sharp for one conversation. Then the conversation ends and it is gone.

- The export only works if you set the date range first. You found that out in
  March, the hard way.
- This client wants a call, never an email. Your agent drafted the email anyway.
- The board deck lives in the shared folder, named by month. It made a new folder.
- The build fails unless one variable is set. That cost you an afternoon, once.
- You tried the other approach last quarter. It was worse. You half remember why.

Each of those was learned once, at real cost. Each one you will explain again,
because the agent that learned it is not the agent you are talking to now.

## The solution

**It learns.** When a session ends, Recall reads it and writes down what it taught.
Not a summary of what happened. The thing that finally worked, the decision and
what you turned down, the approach that looked right and wasn't.

**It brings it back.** The next time that lesson matters, your agent already has
it. Some lessons carry something exact, like a client's name or an error message,
and when that shows up again Recall speaks up on its own. The rest you ask for in
plain words.

**It adds up.** Every session starts further along than the last one. Every lesson
that lands is a dead end your agent does not walk twice, and an explanation you do
not give again.

## Features

- **Speaks up on its own.** A matching lesson appears in the conversation the moment
  it is relevant, without being asked.
- **Answers in plain words.** "How does this client like to be handled" finds the
  answer, even when nothing exact matches.
- **Finds what you decided.** If you keep decision notes, it reads the decision
  inside each one, so "what did we decide about pricing" gets the decision, not the
  document.
- **Remembers what you put off.** A parking lot for later, with a "do next" list
  that holds five items and refuses a sixth.
- **Keeps an honest score.** It tells you when a lesson was shown and did not
  change anything, so you know which ones to rewrite.
- **Says when it has nothing.** No closest-match guesses dressed up as answers.
- **Stays on your machine.** No account, no cloud, no dashboard. Your memory is a
  folder on your own disk, and it never leaves.
- **Backs itself up, privately.** One command makes that folder a private repo
  under your own account.

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

## You can see it working

Nothing happens silently. Each of the three jobs shows itself in your conversation:

**Recalling.** When a lesson matches what you are about to do, a short block appears
in the conversation, right where you are working. It names the lesson and says it is
a prior observation, not an order. You will see it the moment it fires.

**Learning.** At the start of every session, Recall tells you what is waiting to be
learned from your earlier sessions, and whether the last save went through. When it
learns inside a session, it walks through its steps out loud and ends by saying what
it kept and where it put it.

**Saving.** Every write is checked before it is reported. If a save fails, it says
so. It never claims to have kept something it did not.

## It tells you when a lesson did not take

Most memory tools can tell you how often they fired. They cannot tell you whether
it helped, so you get a number that goes up and no idea if anything got better.

Recall keeps score, and it will tell you bad news. On my own memory today, of the
244 repeats it could measure, 213 had been put in front of someone first. The
lesson got read and did not change what happened. That is a writing problem, not a
search problem, and the score is what tells you which lessons to rewrite. I would
rather know that than keep a nicer number.

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

## Install, in detail

The commands at the top are all most people need. This is the rest.

**After a plugin install** there is nothing else to do: the first session makes
`~/.recall` for you, and the desktop app and the terminal share the same setup.

**To get a newer Recall later**, then restart the session:

```bash
claude plugin update recall@ok-wow
```

**Claude Code, from a clone**, if you would rather not use the plugin:

```bash
git clone https://github.com/ok-wow/recall.git ~/recall && cd ~/recall && ./install.sh
```

The installer prints a plan, asks once, and backs up every file it touches. There is
an uninstaller and it puts everything back. Use the plugin or the clone, not both:
with both, every step runs twice, and Recall tells you so at the start of a session.

**Where it does not run.** Cloud and web sessions do not load plugins, so Recall is
off there. It runs where your own `~/.claude` or `~/.codex` folder is.

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
