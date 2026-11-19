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

## Tasks

- **Coding tasks from history**, in the style of SWE-bench: a commit that
  changed code (and ideally tests) becomes a task. The model starts from the
  commit's parent; the commit's own tests, hidden while it works, decide.
- **Validation before use**: a check must fail before the change and pass
  twice with it (catches broken and flaky checks). Prompts must name any exact
  interface the tests rely on, after SWE-bench Verified found that many mined
  tasks were underspecified.
- **Without tests**: the test leader writes a check from the original change
  (validated the same way), or, when the code cannot run, a blind judge
  decides. The method is recorded per result and printed on the card.
- **Review tasks with planted bugs**: one mechanical mutation (flipped
  comparison, swapped `and`/`or`, off-by-one, flipped boolean) in a real recent
  commit's new code. Mutation operators rather than model-invented bugs, so no
  model family plants bugs in its own style; a fixed answer key, so scores do
  not shift as more models are tested. Mining real bug-introducing commits
  (SZZ) was rejected for v0: the research on it reports high noise, and
  vibe-coded histories rarely mark fixes.

## Telling models apart

The first trial on a small library showed the failure to avoid: two models
solved every small task, so the run said "equal" when it only meant "too
easy". Three changes address it:

- **Difficulty mix.** `mine` groups candidates by changed code lines (small
  under 25, medium up to 150, large above) and the test leader picks mostly
  medium, some large, a few small.
- **Quality when both passed.** A blind A/B judgment of solutions that both
  pass, in both orders; a win counts only when both orders agree (the standard
  defence against position bias in pairwise LLM judging, as in MT-Bench and
  Chatbot Arena practice). This is the one place Barton compares models with
  each other, so it is shown on its own card and never changes the solved
  counts, which stay absolute.
- **Ceiling warning.** When every setting solves at least 90 % of the tasks,
  the card says the tasks were too easy instead of "too close to call", and
  the test leader adds harder tasks.

## Fair runs

- **No future leakage.** Each run gets a fresh git repository holding only the
  tree at the task's commit (via `git archive`), outside the user's repo. A
  worktree would expose later commits, including the answer.
- **Web access off** (Claude: `WebFetch`/`WebSearch` disallowed; Codex:
  `web_search` disabled), so a public repo's fix cannot be looked up.
- **The user's own settings are left out** of runs (Claude:
  `--setting-sources project,local --strict-mcp-config`; Codex:
  `--ignore-user-config`), so hooks and MCP servers do not change results; the
  project's own instructions stay in.
- **Unattended but contained.** Claude runs with `--permission-mode dontAsk`
  and a fixed tool list; Codex with its `workspace-write` sandbox (on Windows
  `windows.sandbox="unelevated"`, without which every command is refused).
- **No windows on Windows.** A run starts hundreds of git, shell, CLI and test
  processes. Every one goes through `sp_run`, which on Windows gives it a
  hidden console (`CREATE_NO_WINDOW`) that its own children share, so no
  terminal window flashes up while the user works. Keep it that way: never
  call `subprocess` directly.
- **Errors are not scores.** A run that could not happen (login, model name,
  sandbox, timeout) is recorded as `error`, left out of every chart, and rerun.

## Comparable over time

Tasks are pinned to commits, so a model tested months later solves exactly the
same tasks, however much the code has moved on. Scores are never relative to
other models: a solution is solved or not on its own, and a review finds the
planted bug or not.

## Honest cards

- Counts, not percentages: "7 / 10" shows the sample size; one block per task
  and run makes it visible at a glance.
- "Too close to call" when the gap between the top two is under about 30 % of
  the tasks (at least 2), which is what a handful of tasks can separate.
- The judging method, judge model, runs per setting and the harness ("via
  Codex CLI") are on the card: the result is the model in that harness.
- Cost: Claude Code reports cost; for Codex it is estimated from tokens at list
  prices (`prices.json`) and marked "est.". On a subscription the user may not
  pay per run.
- Credit line: "Run with barton-eval · open source by Kullevi", with the user's
  own name as the author of the result, so it reads as their finding, not
  Kullevi's claim.

## Studied

SWE-bench and SWE-bench Verified (task mining, underspecified and over-narrow
tests), the Aider leaderboard (pass rate with cost, fixed exercise set),
MT-Bench and later work on LLM-judge position and self-preference bias, TREC
pooling bias (why a pooled denominator breaks comparisons over time), the SZZ
evaluation literature, and Defects4J on mutants versus real faults.
