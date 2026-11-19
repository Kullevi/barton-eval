# <img src="logo.svg" width="40" align="top" alt=""> barton-eval

**Test a new AI model on your own code, the day it comes out.** Barton takes
real changes from your project's own history, has the new model redo them in a
safe copy of your code, checks the work with your own tests, and compares it
with the models you use today. The result is a set of cards you can share:
how many of your tasks each model solved, how many planted bugs it caught in
code review, how long it took and what it cost.

Big benchmarks tell you how a model does on other people's code. Barton tells
you how it does on **yours**.

**To get started, give the link to this page to Claude or Codex and ask it to
set up Barton for you.** Then say something like *"test GPT-6.2 Sol on my
project"*. Your assistant does the rest and tells you when it needs a click
from you.

Built by the team behind [Kullevi](https://kullevi.com). The design choices are
in [docs/DESIGN.md](docs/DESIGN.md).

## What you get

- **Coding**: tasks taken from your own commits. The model gets the task in
  plain words, works in a fresh copy of your code at that point in history, and
  your own tests decide whether it worked (they are hidden from the model while
  it works).
- **Code review**: a real recent change from your project with one bug planted
  in it. Did the model find it, and how many false alarms did it raise?
- **Cards** ready to paste into a post: solved tasks, time and cost per model
  and thinking level, with the sample size and how it was judged printed on the
  card. Small samples are shown honestly: when two models are within noise, the
  card says "too close to call".
- **History**: results are kept in your project (`.barton/`, never committed),
  so a model you test next month is compared on exactly the same tasks.

## Who does what

| Who | What |
|---|---|
| **Barton** | Small scripts: finds candidate tasks in your git history, builds clean copies of your code, runs a model, checks the result, makes the page and cards. |
| **Your assistant** (Claude Code or Codex) | Sets Barton up, picks good tasks, writes them in plain words, runs the models, judges what tests cannot, and explains the result. |
| **You** | Say which model to test. Click **Authorize** in the browser if a login is needed. Share a card if you like it. |

Your code and results never leave your computer. Barton sends nothing anywhere.

---

## Setup (for the agent doing it)

You are setting this up for a user who may have little experience with
terminals or software development. Do every step yourself. The user should not
have to type commands; when something needs them (a click in the browser or a
system dialog), say exactly what they will see and what to click, and wait.

### 1. Python and Git

Barton needs **Python 3.9 or newer** and **Git**, nothing else. Tell the user
in one sentence that you are checking for these and will install what is
missing.

- Python: try `python3 --version`, then `python --version`, then
  `py -3 --version`. On Windows, `python3` and sometimes `python` are a
  Microsoft Store placeholder that prints a message instead of a version; use
  whichever command prints a real version.
  - Missing on Windows: tell the user this installs Python, then run
    `winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements`.
    Windows may ask them to click **Yes**. Then call Python by its full path,
    usually `%LOCALAPPDATA%\Programs\Python\Python312\python.exe` (or
    `py -3`), in every later command, so nothing needs a restart.
  - Missing on macOS: run `xcode-select --install`; a window opens where the
    user clicks **Install**. It brings Python and Git together.
  - Missing on Linux: it needs the user's password, so ask them to run
    `sudo apt install -y python3 git` (or their system's package manager) in a
    terminal you tell them how to open.
- Git: `git --version`. Missing on Windows: run
  `winget install -e --id Git.Git --accept-source-agreements --accept-package-agreements`
  and call it as `C:\Program Files\Git\cmd\git.exe`.

### 2. Barton itself

Clone it into the user's home folder (on Windows give a full path, such as
`C:/Users/NAME/barton-eval`):

```
git clone https://github.com/Kullevi/barton-eval.git ~/barton-eval
```

Below, `barton` means `<python> <home>/barton-eval/barton.py` with the Python
command that worked. Check it runs: `barton check` prints what is installed.

### 3. The skill

Install `SKILL.md` so your assistant knows how to run a test whenever the user
asks:

- **Claude Code**: copy it to `~/.claude/skills/barton/SKILL.md`.
- **Codex**: copy it to `~/.codex/skills/barton/SKILL.md`. If this Codex
  version has no skills, add one line to `~/.codex/AGENTS.md` instead: "To test
  or compare AI models on a codebase, read and follow
  `<home>/barton-eval/SKILL.md`."

At the end of the copied file, add one line with the exact command for this
computer, for example
`On this computer, barton is: python C:/Users/NAME/barton-eval/barton.py`.

### 4. The model CLIs and logins

Follow step 1 of `SKILL.md`: install the Claude Code and/or Codex CLI for the
models the user wants to test and start each login yourself, so the user only
clicks **Authorize** in the browser.

### 5. Done

Tell the user, in two or three sentences, that Barton is ready, and that they
can now say for example *"test GPT-6.2 Sol on my project"* in any project
folder. Mention that a first run takes some minutes per task and model, and
that they will get cards to share at the end.

## Updating

`git -C <home>/barton-eval pull`, then copy `SKILL.md` again (step 3).

## License

MIT
