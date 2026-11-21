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
  tasks were underspecified. A fresh judge compares each prompt with what its
  hidden tests assert before any model runs it, and again for any task every
  model failed; a task whose tests want what the prompt leaves open is left
  off the cards with the reason shown (`exclude`), never deleted. In our own
  run on a large private codebase, two of four tasks that every model failed
  were faulty in exactly this way.
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

- **Hard tasks, enough of them.** `mine` groups candidates by changed code
  lines (small under 25, medium up to 150, large above). The test leader picks
  mostly large and medium ones, aiming for the best model to solve about half
  to four fifths, and by default runs 10 coding and 5 review tasks, never
  fewer than 5 of each when the history has them.
- **Quality when both passed.** A blind A/B judgment of solutions that both
  pass, in both orders; a win counts only when both orders agree (the standard
  defence against position bias in pairwise LLM judging, as in MT-Bench and
  Chatbot Arena practice). The two orders may go to judges from different
  vendors; with more judges, every answer must name the same solution.
  `status` shows how often each judge picked the first or second answer, and a
  judge that mostly picks one position can be removed (`forget-judge`).
- **Which tasks told them apart.** The coding card notes how many tasks some
  settings solved and others did not; tasks every setting solved, or none did,
  cannot show a difference. This is the one place Barton compares models with
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
other models: a solution is solved or not on its own, and a review finds the
planted bug or not.

The cards compare every model on the tasks they all have results for. A setting
with results for under half the tasks of the most complete one, such as a run
stopped early, is left off and named instead, so it does not cut everyone else's
comparison down to the few tasks it reached.

## Honest cards

- Counts, not percentages: "7 / 10" shows the sample size, beside a bar of the
  share made.
- Only the result: the shared image carries no settings, judges or dates, and
  the task-by-task table stays folded under the cards.
- "Too close to call" when the top two differ by less than two standard
  errors of the difference between two proportions, or by fewer than two
  tasks: with a handful of tasks most gaps are noise, with fifty they are not.
- The judging method, judge model, runs per setting and the harness ("via
  Codex CLI") are on the card: the result is the model in that harness.
- Cost: Claude Code reports cost; for Codex it is estimated from tokens at list
  prices (`prices.json`) and marked "est.". On a subscription the user may not
  pay per run.
- Credit line: "Run with Barton-eval · open source by Kullevi", with the user's
  own name as the author of the result, so it reads as their finding, not
  Kullevi's claim.

## Studied

SWE-bench and SWE-bench Verified (task mining, underspecified and over-narrow
tests), the Aider leaderboard (pass rate with cost, fixed exercise set),
MT-Bench and later work on LLM-judge position and self-preference bias, TREC
pooling bias (why a pooled denominator breaks comparisons over time), the SZZ
evaluation literature, and Defects4J on mutants versus real faults.
