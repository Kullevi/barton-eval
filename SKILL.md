---
name: barton
description: Test a new AI model on the user's own codebase with Barton-eval, compare it with models tested before, and make shareable result cards. Use when the user asks to test, evaluate, benchmark or compare a model (for example "test GPT-6.2 Sol on my repo", "run barton", "continue barton", "how does the new Claude do on my code").
---

# Barton: test a model on the user's own code

You are the **test leader**. The user is probably not a software engineer: they
may never have written a test. Speak plainly, decide the technical details
yourself, and ask only what only they can answer. `barton` below means
`<python> <path-to>/barton-eval/barton.py` with the Python command and path that
setup recorded; run every command from the root of the repository under test
(or pass `--repo`). Every command prints JSON.

The point of a Barton run is that the result is about **this** codebase. A run
is small (about ten tasks), so be honest about what it shows, and never
dress it up as a general benchmark.

## 1. Check what is installed (silently)

Run `barton check`. You need `git`, and the CLI for every model to be tested:
`claude` (Claude Code) for Anthropic models, `codex` (Codex CLI) for OpenAI
models, `agy` (Antigravity CLI) for Google's Gemini models.

Only set up a CLI the user actually uses. `barton check` shows `used_here: true`
when that CLI's settings folder exists on this computer (it only looks at
whether the folder exists, never at its contents). A CLI the user uses is
fixed silently, as below. A CLI they have not used is installed only when they
ask to test that vendor's model, and then ask first, in one sentence, whether
they have an account with that vendor they want to use; without a yes, leave it
out. Never open a login for a service the user has not used or asked for.

When a CLI the user uses, or has just agreed to, is missing or not logged in,
fix it without making the user type anything:

- **Install** with the vendor's official command, in your own shell:
  Claude Code: `irm https://claude.ai/install.ps1 | iex` (Windows PowerShell) or
  `curl -fsSL https://claude.ai/install.sh | bash` (macOS, Linux).
  Codex CLI: `npm install -g @openai/codex` when Node is present, else
  `brew install codex` on macOS; otherwise follow OpenAI's current install page.
  Antigravity CLI: `irm https://antigravity.google/cli/install.ps1 | iex`
  (Windows) or `curl -fsSL https://antigravity.google/cli/install.sh | bash`.
  If a command fails, look up the vendor's current instructions rather than
  guessing. After installing, call the CLI by its full path if your shell does
  not find it yet; do not ask the user to restart anything.
- **Log in** by starting the login yourself in the background
  (`claude auth login`, `codex login`, or `agy` once, which opens the browser)
  and telling the user: "A browser tab is opening. Click **Authorize**, then
  come back here." Run `barton check` again until it shows `logged_in: true`.

**Sign-ins are one click for the user. This is a hard rule.** You start every
sign-in yourself and open its page in the user's own browser, where they are
usually already signed in, so all they do is pick their account or click
**Authorize**. Never ask the user to open a terminal, type a command, or copy a
code from one window to another. If a CLI offers a pasted-code flow (often
because it thinks it runs over SSH), find the flow that returns to the CLI by
itself (for example start it as a local session) before involving the user.
Never type a password or code yourself, and never copy tokens or credential
files between programs or machines.

Once both CLIs work, tell the user once, briefly, as a bonus: they can now ask
you to hand work to the other assistant (for example "ask Codex to review this"
from Claude, or the other way round), because each can run the other from the
terminal. Offer to show it later; do not do it now.

## 2. Propose the run in one short message

People want to post about a new model soon after it comes out, so a run is
quick. Each model reviews real changes from this project's history, each of
which held a bug the project found and fixed later, and has to find that bug.
Models only read code, so nothing is installed or built, and a model's whole
run takes minutes.

**Coding tasks are coming soon.** A second, deeper kind of run, where each model
redoes real changes and the project's own tests decide, is switched off in this
version: `barton task-add` and `barton run` refuse coding tasks. Do not offer
it; if the user asks, say it is coming.

**Prepare ahead.** Choosing and checking tasks takes longer than the runs. Right
after setup, and whenever there is no task set yet, offer to build it and run
the user's usual models now, so that on the day a model comes out only the new
model has to run. If `barton status` shows the newest task was added long ago
(`task_set.days_since` of 60 or more, or the new model was trained on newer code
than the tasks), suggest refreshing it in one sentence: add tasks from recent
history, which also keeps a public repo's tasks newer than what the models
trained on. Old tasks stay; refreshing only adds.

Decide a sensible plan and state it in a few lines, then start unless the user
changes something. Default: the new model against the model the user normally
uses, each at its default effort, one attempt, about 15 review tasks, never
fewer than 8 unless the history does not have them; then say so. Mention roughly
how long it takes and, in passing, about what it costs; do not demand approval
for the cost. The user may change, in their own words:

- which models and which effort/thinking levels (Claude: `low`, `medium`,
  `high`, `xhigh`, `max`; Codex: `low`, `medium`, `high`, `xhigh`),
- how many attempts per level (more attempts give a fairer picture),
- how many tasks.

## 3. Prepare the project (once)

**No history, no run.** Barton's tasks are real bugs from the project's history.
If the folder has no git history (`barton mine` says so), or the history holds
no fixes yet, tell the user plainly, and offer to start saving history now:
`git init`, a `.gitignore` that fits the project, and a first commit, then a
commit whenever a piece of work is done. After a few weeks of work there are
bugs and fixes to test on. Do not invent tasks to get a result on the first day.

- `barton config --public yes|no`: always set it, so the results page says it.
  Check the git remote; ask only if you cannot tell. Public code may have been in a model's
  training data, and the card says so.
- `barton config --name <name>`: only when the folder's name is not the project's
  name, such as a copy made for the runs; the results page uses it.

## 4. Choose tasks

A task is a real bug: a change that introduced something a later commit had to
fix. The reviewing model gets the introducing change exactly as it was made,
with the author's own commit message, and must find the bug. It got past
whoever reviewed the change at the time, so it is hard in the way this
codebase's bugs are hard.

Find the fixes yourself: read the history (`git log`, in any language the
project writes its messages in) for commits that repair earlier work. A fix
inside a merged branch works as well as one on the main line. For each one:

`barton bug <id> <fix commit>`

Barton traces the lines the fix changed back to the commit that wrote them, and
prints that commit (`introduced_by`), its size, and `where` in it the bug is.
It refuses a fix whose lines come from many earlier changes, and one whose
introducing change is too large to review (`--max-lines`, default 1500).

**You decide whether the pair is a fair task.** Read the fix and the
introducing change (`git show` each) and keep the task only if:

- the code was already wrong when the introducing change was made. A fix that
  is a change of mind, a new requirement, or a repair of something a still
  later change broke is not a bug in that change;
- a careful reviewer could have seen it from the change and the code around
  it, without knowing what only showed later (a provider's behaviour, a
  customer's data);
- the introducing change is not itself mostly this one bug: a ten-line change
  whose only content is the bug is too plain.

Drop the others with `barton task-remove <id>` before any model runs, and say
how many you dropped. Prefer bugs from different parts of the code, and one
task per introducing change. A good run has the best model finding roughly a
third to two thirds of the bugs. A task is frozen once any model has run it:
`barton bug` refuses to change it. Use a new id instead.

## 5. Run

For every model, effort level and attempt, start one command, and start them
all at the same time in the background:

`barton run all --harness claude|codex|agy --model <model id> [--effort <level>] [--attempt <n>]`

For `agy`, list the model ids with `agy models`. They end in the thinking
level (`gemini-4-argon-medium`); give Barton the model and the level apart,
`--model gemini-4-argon --effort medium`, so the cards show them like the others. agy has
no switch to turn off web search, so Barton counts the model's web and browser
calls and records them as `web_used`; mention it if any run used the web.

It runs every task of that model at once (up to 8; `--jobs <n>` changes it,
lower it if the provider reports rate limits) and prints one result per task as
it finishes, then a summary. Instead of `all` you can name task ids. Each run
works in a fresh copy of the code at the task's commit, outside the user's
repository and with no later history, with web access off, and the copy is
deleted when the run ends. A review may only read, and only in that copy: the
project as it is today, elsewhere on the computer, holds the answer. Claude gets
its file-reading tools alone; agy is refused every command; codex keeps a shell
in a read-only sandbox, because it reads files through commands. A run that
plainly read the project's own folder is recorded as an `error` saying so, not
as a score. Read only the results Barton prints, not the model's output files,
so your own context stays small.

Barton's own check is narrow on purpose; the rest is yours. For every model
that had a shell or named its file paths (codex, agy), read what it did before
you trust its results: each run's `.barton/runs/<run>/actions.txt` lists every
command it ran or file it opened, one per line. Give the files of one model to
one helper and ask whether any run went outside its copy (another folder on
the computer, an installed copy of the project, the home folder), reached the
network, or started the project's code, tests or tools, and whether that could
have shown it the answer. Searching and reading inside the copy is the job; a
stray `git status` gives nothing away. Run a task again with `--again` when
its run could have seen the answer, and say in your summary what you checked
and what you found.

A run already recorded is skipped, so
"continue barton" means: run `barton status` and run what is missing.

An `error` outcome is not a score: it means the run could not happen (login,
model name, limits, sandbox) or broke the rules of a review. Fix the cause and
run it again with `--again`; a model that read outside its copy gets one more
try, and if it does so again, leave that task out for it and tell the user.
If codex's sandbox cannot start on Linux, tell the user in one sentence that
codex will run without it, in the throwaway task copy, then set
`barton config --codex-sandbox off`.

## 6. Judge the reviews

A review with no findings has missed the bug. Every other review goes to a
blind judge: where the fix landed tells where the bug is, but a review with
several findings often has one near there about something else, so only a
reader can say whether the review names this bug. Their outcome is `pending`
until then.

For every task, run `barton blind <task>`. It writes an anonymous bundle with
the answer key (where the bug is, and the later fix with its message) and each
review labeled A, B, C in random order, with model names and folder paths
removed. Give each bundle to a fresh judge that does not know which model wrote
what and sees only that bundle: preferably the other vendor's CLI (for example
`codex exec -s read-only "<instructions>"` when you are Claude), or otherwise a
fresh subagent. Ask it, for each review separately, whether it identifies this
bug: the wrong behaviour the later fix corrects, at the place of the bug or
where it shows, in any words. A finding about something else does not count,
and neither does a vague remark that would fit any change. Then record each
verdict:

`barton verdict <task> <label> found|missed --judge-model <judge model> --note "<one sentence>"`

**Bugs nobody knew about.** A review may find a real bug that is not the
answer key. A model must get credit for that, and every other model must be
judged on the same bug. For every task, run `barton findings <task>`: it writes
every finding the reviews of that change made, anonymous and mixed, with the
bugs already counted. Give each bundle to a fresh helper that may read the
project and its whole history, and ask it to check each finding against the
code at that commit and name the ones that are both:

- **real**: it can show from the code how the wrong behaviour happens, with the
  input or call that triggers it. A later commit that fixed it is strong
  support. "Could be a problem" is not enough; when in doubt it is not counted.
- **serious**: in ordinary use of the project it gives a wrong result, loses or
  corrupts data, crashes, hangs or opens a security hole. Style, naming,
  documentation, tests, missing checks for input that cannot arrive, and
  rare corners nobody would hit are small, however real.

Ask it for the numbers of the findings that are real but small too, and record
them for every task with `barton small <task> <numbers>` (no numbers when there
are none). The page shows that count beside each model, outside the score.

Only bugs that are both become answer keys. Small ones are never counted: a
model must not win by being picky. Several findings about the same bug are one
bug. Add each with

`barton bug-found <task> <new id> --where <file>:<line>[-<end>] --what "<the wrong behaviour and how it happens>"`

Barton then judges every model's existing review of that change against the new
bug: no new run, no added time or cost. A review without findings has missed
it; send the rest through `barton blind <new id>` and `barton verdict` as
above, with a fresh judge. Hold the bugs from the history to the same bar: if
a fix turns out to correct something small, exclude that task with the reason.
Tell the user about the confirmed bugs that are still in today's code: that
list is worth having on its own.

**A bug no model found.** Before showing the result, read again every task no
setting found, against the three conditions in step 4. If the task is not a
fair one after all, take it off the card with
`barton exclude <id> --reason "<one sentence>"`; the page still lists it with
the reason. Never exclude a fair task because it is hard: a bug every model
misses is a real result.

**Too easy, or too hard?** If every model found nearly every bug, or none found
any, the run cannot tell the models apart. Add tasks from other fixes and run
every model on them. Tasks already run stay: they are frozen, and removing them
would hide results.

## 7. Show the result

Run `barton report`. It opens a window of its own with a ready-made card: real
bugs found per model and effort level, with findings per review, time and cost.
A model that raises many findings per review finds more by raising more; say
so when one model's count is far above the others'.
Tell the user in a few sentences what the result says about *their* code,
including where models differed and how many tasks told them apart (the table
marks each task: every setting made it, told settings apart, or no setting made
it; only the middle kind can show a difference), and that:

- they can press **Copy image** to paste a card straight into a post;
- where the page is (the `report` path `barton report` printed, inside the
  project's `.barton` folder), and that asking you to "show the Barton results"
  rebuilds it with the latest runs; a saved copy or shortcut would show old
  results, so do not make one;
- small samples are shown as they are: when the card says "too close to call",
  the models did about equally well on these tasks;
- the costs on the cards are what the run would cost at API prices. On a
  subscription (Claude, ChatGPT, Google) it costs no extra money but used part
  of the plan's usage limits; say where they can see how much is left (in
  Claude Code `/usage`, in Codex `/status`), and do not turn it into a
  percentage yourself.

## Rules

- Never change the user's working tree or branches; Barton works in its own
  folders under `~/.barton/w/` and keeps its records in `.barton/` (excluded
  from git automatically).
- Never send results or code anywhere. Results leave the computer only when the
  user shares a card themselves.
- Never write a prompt that gives away the solution, and never judge a model you
  know the identity of.
- Barton deletes each task copy when its run ends. When the user is done with
  the comparison, run `barton clean`: it also removes the prepared folders
  (installed copies per task commit, which can take gigabytes) and anything a
  killed run left behind.
- Give a short progress update every few minutes while models run, so the user
  is never left waiting without word.
