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
is small (a handful of tasks), so be honest about what it shows, and never
dress it up as a general benchmark.

## 1. Check what is installed (silently)

Run `barton check`. You need `git`, and the CLI for every model to be tested:
`claude` (Claude Code) for Anthropic models, `codex` (Codex CLI) for OpenAI
models. If one is missing or not logged in, fix it without making the user type
anything:

- **Install** with the vendor's official command, in your own shell:
  Claude Code: `irm https://claude.ai/install.ps1 | iex` (Windows PowerShell) or
  `curl -fsSL https://claude.ai/install.sh | bash` (macOS, Linux).
  Codex CLI: `npm install -g @openai/codex` when Node is present, else
  `brew install codex` on macOS; otherwise follow OpenAI's current install page.
  If a command fails, look up the vendor's current instructions rather than
  guessing. After installing, call the CLI by its full path if your shell does
  not find it yet; do not ask the user to restart anything.
- **Log in** by starting the login yourself in the background
  (`claude auth login`, or `codex login`) and telling the user: "A browser tab
  is opening. Click **Authorize**, then come back here." The browser is usually
  already signed in to claude.ai or ChatGPT, so it is one click. Run
  `barton check` again until it shows `logged_in: true`. The desktop apps do not
  always share their login with the CLI; never copy tokens or credential files
  between them.

Once both CLIs work, tell the user once, briefly, as a bonus: they can now ask
you to hand work to the other assistant (for example "ask Codex to review this"
from Claude, or the other way round), because each can run the other from the
terminal. Offer to show it later; do not do it now.

## 2. Propose the run in one short message

Decide a sensible plan and state it in a few lines, then start unless the user
changes something. Default: the new model against the model the user normally
uses, each at its default effort, one attempt, about 10 coding tasks and 5
review tasks. Never fewer than 5 of each unless the history does not have them;
then say so. Mention roughly how long it takes (a few minutes per task and
model) and, in passing, about what it costs; do not demand approval for the
cost. The user may change, in their own words:

- which models and which effort/thinking levels (Claude: `low`, `medium`,
  `high`, `xhigh`, `max`; Codex: `low`, `medium`, `high`, `xhigh`),
- how many attempts per level (more attempts give a fairer picture),
- coding tasks, review tasks or both, and how many.

Presets you can offer by name: **quick** (6 coding and 3 review tasks, 1
attempt), **standard** (10 and 5, 1 attempt), **thorough** (16 and 6, 3
attempts).

## 3. Prepare the project (once)

Look at the repository yourself and decide how its code and tests run.

- If the project needs installed packages to run its tests, set them up:
  `barton config --setup "<install command>"` (runs in each task folder), or,
  faster, `barton config --link node_modules` to reuse the user's installed
  folder. Untracked files the code needs, such as `.env`, go in
  `barton config --copy .env` (copied into task folders only, never committed).
- `barton config --public yes|no`: whether the repo is public (check the git
  remote; ask only if you cannot tell). Public code may have been in a model's
  training data, and the card says so.

## 4. Choose tasks

Run `barton mine`. It lists commits that changed code, grouped as `small`,
`medium` and `large` by how much code they changed, newest and those with tests
first. **Choose hard tasks**: mostly large and medium, few small. Current
models solve nearly every small task, and a run where every model gets full
marks tells the user nothing. Aim for tasks the best model may fail: bug fixes
whose cause is not obvious from the description, behaviour changes touching
several functions or files, and edge cases the tests pin down. A good run has
the best model solving roughly half to four fifths of the tasks. For each task you keep, write a JSON file and
run `barton task-add <file>`:

```json
{"id": "t1", "kind": "write", "base": "<sha>^", "reference": "<sha>",
 "prompt": "<the task, in plain words>",
 "check": {"command": "<test command>", "files": ["<test files from the commit>"],
           "env": {"PYTHONPATH": "src"}, "method": "tests"}}
```

Rules for a good task:

- **The prompt** describes what to achieve as a user or issue would, not how the
  author did it. Never paste or paraphrase the solution. If the tests depend on
  an exact name, signature, message or file, state it in the prompt; otherwise
  a correct solution fails for a guessed name.
- **The check**, best first:
  1. the project's own tests that the commit added or changed (`files` lists
     them; they are hidden from the model and added only when checking;
     `method: "tests"`);
  2. if there are none, write a small check yourself from the original change
     and put it inline: `"write": {"<path>": "<file content>"}`,
     `method: "check"`;
  3. if the code cannot run at all, leave out `check`: the solution is judged
     blind (step 6).
- Run `barton validate <id>` for every task with a check. It must fail before
  the change and pass twice with it. Drop tasks that fail validation and say how
  many you dropped.
- Prefer tasks from different parts of the code, of the kind the user actually
  does. If the history has too few usable commits (common: "fix stuff" commits
  touching many files), write realistic tasks from the current code instead
  (`"base": "HEAD"`, no `reference`, no `check`), judged blind.

**Review tasks**: `barton plant <id> <sha> --check "<test command>" [--env NAME=VALUE]`
(the same environment as the coding checks, such as `--env PYTHONPATH=src`) takes a
real commit and plants one mechanical bug in its new code (a flipped
condition, an off-by-one, a swapped `and`/`or`). With `--check` it keeps only a
bug that makes the project's tests fail, so it is surely a real bug; small
commits often have no place for one, so try commits with more logic. Without
tests, leave out `--check` and look at what it planted yourself: if the change
is harmless or in dead code, run it again with `--seed <n>`. A reviewing model
gets the commit and must find the planted bug; the answer key is fixed, so
results stay comparable.

A task is frozen once any model has run it: `task-add` and `plant` refuse to
change it. Use a new id instead.

## 5. Run

For every task, model, effort level and attempt:

`barton run <task> --harness claude|codex --model <model id> [--effort <level>] [--attempt <n>]`

Each run works in a fresh copy of the code at the task's commit, outside the
user's repository and with no later history, with web access off. Run at most
two at a time. Read only the JSON line each run prints, not the model's output
files, so your own context stays small. A run already recorded is skipped, so
"continue barton" means: run `barton status` and run what is missing.

An `error` outcome is not a score: it means the run could not happen (login,
model name, limits, sandbox). Fix the cause and run it again with `--again`.

## 6. Judge what has no check

For tasks with outcome `pending`, run `barton blind <task>`. It writes an
anonymous bundle with the task, the original author's change as a reference,
and every solution labeled A, B, C in random order, with model names and
folder paths removed. Reviews also end up here when a model's reply ends without
a JSON bug list; then the bundle holds
the answer key and each review, and the judge decides `found` or `missed`. Give the bundle to a judge
that does not know which model wrote what: preferably the other vendor's CLI
(for example `codex exec -s read-only "<instructions>"` when you are Claude), or
otherwise a fresh subagent. Ask it, for each solution separately, whether it
fully does what the task asks and would work, judging each on its own merits
and not by how close it is to the reference, which is only one valid way. Then
record each verdict:

`barton verdict <task> <label> solved|failed|found|missed --judge-model <judge model> --note "<one sentence>"`

**Quality when both passed.** Tests say only pass or fail. For every coding
task that two or more settings solved, run `barton pairs <task>`: it writes one
blind A/B bundle per pair of solutions, each pair in both orders. Judge **each
bundle with a fresh judge that sees only that bundle** (a new subagent or a new
`codex exec` per bundle; a judge that sees several bundles compares across them
and is no longer independent). Record each answer:

`barton prefer <pair> first|second|tie --judge-model <judge model> --note "<one sentence>"`

A win counts only when both orders agree; otherwise it is a tie, which cancels
the judge's habit of favouring the first or the second answer.

**Too easy?** If the best setting solved 90 % or more of the coding tasks, or
every model found every planted bug, the run cannot tell the models apart. Before
you show the result, add harder tasks (larger commits, bug fixes, changes
across several files; for reviews, commits with more logic) and run every model
on them, until the best setting is clearly below full marks. Tasks already run
stay: they are frozen, and removing them would hide results.

## 7. Show the result

Run `barton report`. It opens a page in the browser with ready-made cards:
tasks solved, better solution when both passed, planted bugs found, time and
cost per model and effort level.
Tell the user in a few sentences what the result says about *their* code,
including where models differed, and that:

- they can type their name or handle on the page, then press **Copy image**
  to paste a card straight into a post;
- small samples are shown as they are: when the card says "too close to call",
  the models did about equally well on these tasks.

## Rules

- Never change the user's working tree or branches; Barton works in its own
  folders under `~/.barton/w/` and keeps its records in `.barton/` (excluded
  from git automatically).
- Never send results or code anywhere. Results leave the computer only when the
  user shares a card themselves.
- Never write a prompt that gives away the solution, and never judge a model you
  know the identity of.
- Clean up with `barton clean` if runs were interrupted.
