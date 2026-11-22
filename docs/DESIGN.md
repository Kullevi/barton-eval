# Design

Barton answers one question for one person: how does this new model do on
**my** code, compared with what I use now? Large public benchmarks already
answer the general question carefully. Barton's value is that it is personal,
quick to run, and honest about being small.

## Shape

- **The assistant is the test leader.** Picking good tasks, phrasing them,
  deciding how a project's tests run and judging work without tests all need
  judgment, so a Claude Code or Codex session does them, guided by `SKILL.md`.
  `barton.py` does only the mechanical parts, so they are the same every time.
- **No server, no dependencies.** Python standard library and one static HTML
  page. Installed by handing the README to an assistant, as with
  [Girdish](https://github.com/Kullevi/girdish).
- **Nothing leaves the computer.** No telemetry. A crowdsourced ranking was
  considered and rejected for now: across very different personal repos it
  would mostly measure which repos people chose and which results they
  submitted, and silent collection from private code would cost the trust the
  tool depends on. The shared card is the way results spread.

## Quick first

Barton exists to be run the day a model comes out, and to give something worth
sharing the same day. The first version asked every model to redo real changes
and let the project's tests decide. That is the strongest evidence, but on a
large codebase each task needs a full install and build, and a run took hours.
So the released kind of run is review only: models read code and write a
review, nothing is built per model, and a run takes minutes. Coding tasks stay
in the code, switched off (`CODING_TASKS`), and come back as an optional deeper
run.

## Tasks

- **A real bug, found by its fix.** A review task is the change that introduced
  a bug the project fixed later. The test leader finds fix commits by reading
  the history; `barton bug` traces the lines a fix changed (`git blame` on the
  fix's parent) to the commit that wrote most of them, which is the task. The
  reviewer gets that commit as it was made, with its message. This is SZZ, with
  the step the research finds noisy, deciding which commits are fixes and
  whether the blamed commit really introduced the bug, done by a reader and not
  by a rule: the test leader keeps a pair only if the code was already wrong
  then, and a reviewer could have seen it.
- **What was tried first.** Mechanical mutations (a flipped comparison, a
  swapped `and`/`or`) in a real commit: on a large private codebase every
  model, also at the lowest effort, found five or six of seven. Then a real
  change with one part left out, so the bug was the project's own earlier code
  between two parts of the system: five settings, from the cheapest model to
  the most capable, found 11 to 13 of 13 or 14, on that codebase and on a
  public library. A change with a part missing gives itself away, because the
  rest of the change and its message point at the gap. On real bugs the same
  settings found 3 to 9 of 16 or 21, in the order one would expect. Bugs that
  got past the review of their day are hard; bugs made for the test are not.
- **The blamed commit, not its merge.** A merge of a long branch is thousands
  of lines. The commit that wrote the lines is the unit a person reviewed.
- **A judge for every review with findings.** Reviews differ in how much they
  raise: one model averaged six findings per review where another averaged
  two. With many findings one lands near the fixed lines by chance, so place
  alone is not a find. A blind judge reads each review against the fix. A
  review with no findings is a miss without a judge. The card shows findings
  per review beside the score.
- **History is required.** A project without git history, or with no fixes in
  it yet, gets no run: the test leader offers to start saving history, and
  invents no tasks to have a result on the first day.
- **Coming soon: coding tasks from history**, in the style of SWE-bench: the
  model starts from a commit's parent and the commit's own tests, hidden while
  it works, decide. A check must fail before the change and pass twice with it,
  and a fresh judge compares each prompt with what its hidden tests assert,
  after SWE-bench Verified found that many mined tasks were underspecified.

## Telling models apart

The first trial on a small library showed the failure to avoid: two models
solved every small task, so the run said "equal" when it only meant "too
easy".

- **Hard tasks, enough of them.** The test leader aims for the best model to
  find about a third to two thirds of the bugs, and by default runs about 15
  tasks, never fewer than 8 when the history has them.
- **Which tasks told them apart.** The card notes how many tasks some settings
  made and others did not; tasks every setting made, or none did, cannot show
  a difference.
- **Ceiling warning.** When every setting makes at least 90 % of the tasks,
  the card says the tasks were too easy instead of "too close to call", and
  the test leader adds harder tasks.

## Fair runs

- **No future leakage.** Each run gets a fresh git repository holding only the
  tree at the task's commit (via `git archive`), outside the user's repo. A
  worktree would expose later commits, including the answer.
- **Web access off** (Claude: `WebFetch`/`WebSearch` disallowed; Codex:
  `web_search` disabled), so a public repo's fix cannot be looked up.
- **A review only reads, and only its copy.** The first review runs let models
  run commands. Three things followed: on a public library, tests the models
  ran imported the copy of that library installed on the computer, a later
  version with the bug fixed; on a private codebase, a model followed the
  project's own instructions to the real project folder, where today's code
  holds the answer; and a Python the model started crashed into an error box
  on the user's screen. A review needs none of this, so the change is given as
  a file (`CHANGE_UNDER_REVIEW.diff`) and each CLI is held to reading as far as
  it can be: Claude gets `Read`, `Grep` and `Glob` alone, and its permission
  mode refuses reads outside the folder; agy is refused every command, write
  and URL by deny rules; Codex reads files through shell commands, so it keeps
  a shell, in its read-only sandbox. agy's strict mode refused reads inside the
  folder at random in print mode, and Codex's Windows sandbox cannot restrict
  reads, so what a review read is checked afterwards: agy names every path its
  tools open, and a Codex command that names the project's own folder is found
  in its text. Such a run is an error, not a score.
- **The user's own settings are left out** of runs (Claude:
  `--setting-sources project,local --strict-mcp-config`; Codex:
  `--ignore-user-config`), so hooks and MCP servers do not change results; the
  project's own instructions stay in.
- **Unattended but contained.** Claude runs with `--permission-mode dontAsk`
  and a fixed tool list; Codex with its `workspace-write` sandbox (on Windows
  `windows.sandbox="unelevated"`, without which every command is refused).
  Where Linux does not let that sandbox start, Codex can run without it
  (`config --codex-sandbox off`): the model still works in a throwaway copy.
- **Stopping cleans up.** Model CLIs and checks run in sessions of their own,
  which a Ctrl-C of Barton does not reach. On a stop Barton starts nothing more,
  stops everything it started (on Windows also through a job object that ends
  with Barton), removes its task folders and records nothing, so the cut-short
  runs simply run again next time.
- **No windows on Windows.** A run starts hundreds of git, shell, CLI and test
  processes. Every one goes through `sp_run`, which on Windows gives it a
  hidden console (`CREATE_NO_WINDOW`) that its own children share, so no
  terminal window flashes up while the user works. Keep it that way: never
  call `subprocess` directly.
- **Errors are not scores.** A run that could not happen (login, model name,
  limits, sandbox) is recorded as `error`, left out of every chart, and rerun.
  Running out of time is the model's own result: the task counts as not solved.

## Comparable over time

Tasks are pinned to commits, so a model tested months later solves exactly the
same tasks, however much the code has moved on. Scores are never relative to
other models: a review finds the bug or not, on its own.

The cards compare every model on the tasks they all have results for. A setting
with results for under half the tasks of the most complete one, such as a run
stopped early, is left off and named instead, so it does not cut everyone else's
comparison down to the few tasks it reached.

## Honest cards

- Counts, not percentages: "7 / 10" shows the sample size, beside a bar of the
  share made.
- Only the result: the shared image carries no settings, judges or dates, and
  the task-by-task table stays folded under the cards.
- "Too close to call", noted on the card, when the top two differ by less than two standard
  errors of the difference between two proportions, or by fewer than two
  tasks: with a handful of tasks most gaps are noise, with fifty they are not.
- The harness ("Codex CLI") and runs per setting are on the card: the result
  is the model in that harness.
- Cost: Claude Code reports cost; for Codex and the Antigravity CLI it is
  estimated from tokens at list prices (`prices.json`) and marked as an
  estimate. On a subscription the user may not pay per run.
- Above the results, two plain sentences on how Barton tests, not a verdict;
  below them, one credit line: "Run with Barton-eval · by Kullevi" and the
  repository link.
- The page is plain HTML, so its text can be selected and its link followed;
  only the shared image is drawn on a canvas.

## Studied

SWE-bench and SWE-bench Verified (task mining, underspecified and over-narrow
tests), the Aider leaderboard (pass rate with cost, fixed exercise set),
MT-Bench and later work on LLM-judge position and self-preference bias, TREC
pooling bias (why a pooled denominator breaks comparisons over time), the SZZ
evaluation literature, and Defects4J on mutants versus real faults.
