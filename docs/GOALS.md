# Barton goals

This is the target: what Barton must achieve and what it deliberately leaves
out. `DESIGN.md` explains the choices behind it.

## Purpose

When a new AI model comes out, people who build with Claude Code or Codex want
to know one thing: is it better on **my** project than what I use now? Public
benchmarks measure other people's code. Barton answers the question on the
user's own codebase, quickly, honestly about its small sample, and in a form
they can share.

## Who it is for

Someone who builds with AI and may never have written a test. They have a
project in git and a Claude or Codex session. They should not need to read
code, type commands or understand how evaluations work.

## The outcome

1. **Setup from a link.** The user gives this repository's link to Claude or
   Codex. The agent reads the README, installs Barton and its skill, installs
   and logs in the model CLIs with at most a click from the user, and says it
   is ready.
2. **A test from one sentence.** The user says "test <model> on my project".
   The agent, guided by `SKILL.md`, proposes a run in one message, finds real
   bugs in the project's history and checks that each is a fair task, runs the
   models, has their reviews judged blind, and opens the results. A model's run
   takes minutes.
3. **Shareable, honest cards.** The results page draws cards with raw counts,
   the judging method and the sample size printed on them, credited to the
   user and to Barton-eval, ready to copy into a post.
4. **Comparable over time.** A model tested months later solves exactly the
   same tasks and is shown next to the earlier results.

## What Barton does

- **Trace real bugs**: from a commit that fixed a bug to the change that
  introduced it, which becomes a review task with the fix as its answer key.
- **Build fair task folders**: the code at the task's commit, with no later
  history, outside the user's repository.
- **Run** one model on one task through its own CLI, unattended, reading only
  and only in its task folder as far as the CLI can be held to it, with web
  access off and the user's own CLI settings left out, and record the outcome,
  time, tokens and cost.
- **Prepare blind judging**: anonymous bundles of reviews with the answer key.
- **Report**: a static page with a card that can be copied or saved as PNG.
- **Coming soon: coding tasks**, where a model redoes a real change and the
  project's own tests decide. The code is there, switched off.

## Left to the agent, not built into Barton

- Finding the fixes in the history, and deciding whether each bug is a fair
  task: already wrong when the change was made, and visible to a reviewer.
- Judging blind bundles (one fresh judge per bundle).
- Explaining the result to the user in plain words.

Barton has no server, no accounts, no leaderboard, no telemetry and no
background service.

## Constraints

- One Python 3.9+ file plus the results page, standard library only, and the
  system `git`. No other dependencies.
- Windows 10/11, macOS and Linux. On Windows, no console window appears for
  any process Barton starts.
- Never changes the user's working tree, branches or history.
- Nothing leaves the computer except through the model CLIs the user chose and
  the cards the user shares.
- MIT licence.

## Done when

- A fresh Claude Code session and a fresh Codex session, given only the link,
  each set Barton up on a clean machine, with no help beyond the user's clicks
  to authorize logins.
- Asked to test a model, the agent completes a run on a real project, with
  real bugs as review tasks, and opens a page whose card matches the recorded
  results.
- A run with no difference between models says "too close to call" or "too
  easy", never a winner.
- No terminal window appears on Windows during setup or a run.
