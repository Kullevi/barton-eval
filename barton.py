#!/usr/bin/env python3
"""barton-eval: test a new AI model on your own codebase and share the result.

A Claude Code or Codex session is the test leader (see SKILL.md). This script
does the mechanical parts it asks for: checking the CLIs, mining task
candidates from git history, building clean task folders with no future
history, running a model headless, checking its work, recording results and
building the report page. Standard library only. See docs/DESIGN.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tarfile
import time
import webbrowser
from pathlib import Path

VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent
# Task folders live outside the user's repo, on a short path (Windows MAX_PATH).
WORK_ROOT = Path(os.environ.get("BARTON_WORK", Path.home() / ".barton" / "w"))
PRICES_PATH = HERE / "prices.json"
GIT_ID = ["-c", "user.name=barton", "-c", "user.email=barton@localhost",
          "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false"]
RUN_TIMEOUT = 45 * 60
CHECK_TIMEOUT = 15 * 60
REVIEW_FILE = "barton-review.json"

TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]+\.py$|_test\.(py|go|rb)$"
    r"|\.(test|spec)\.[jt]sx?$|Tests?\.(java|kt|cs|swift)$")
CODE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rb",
            ".rs", ".java", ".kt", ".cs", ".php", ".swift", ".c", ".cc", ".cpp",
            ".h", ".hpp", ".vue", ".svelte", ".dart", ".scala", ".ex", ".exs"}


# ---------------------------------------------------------------- helpers

def out(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def fail(msg: str, code: int = 1) -> None:
    out({"error": msg})
    sys.exit(code)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def git(repo: Path, *args: str, check: bool = True, binary: bool = False):
    p = subprocess.run(["git", "-C", str(repo), *GIT_ID, *args],
                       capture_output=True, stdin=subprocess.DEVNULL)
    if check and p.returncode != 0:
        fail(f"git {' '.join(args)} failed: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout if binary else p.stdout.decode("utf-8", errors="replace")


def which(name: str) -> str | None:
    return shutil.which(name)


def run_cmd(cmd: list[str] | str, cwd: Path, timeout: int, shell: bool = False,
            env: dict | None = None):
    """Run with stdin closed; return (code, stdout, stderr, seconds, timed_out)."""
    t0 = time.time()
    full_env = {**os.environ, **env} if env else None
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, shell=shell, env=full_env,
                           stdin=subprocess.DEVNULL, timeout=timeout)
        return (p.returncode, p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"), time.time() - t0, False)
    except subprocess.TimeoutExpired as e:
        so = (e.stdout or b"").decode("utf-8", "replace")
        se = (e.stderr or b"").decode("utf-8", "replace")
        return (-1, so, se, time.time() - t0, True)


def resolve_cli(name: str) -> list[str]:
    """Full command for a CLI; on Windows npm shims are .cmd files."""
    path = which(name)
    if not path:
        fail(f"'{name}' was not found. Run `barton check` for how to install it.")
    return [path]


# ---------------------------------------------------------------- state

class Barton:
    """The .barton folder inside the repo under test."""

    def __init__(self, repo: Path):
        self.repo = repo.resolve()
        if git(self.repo, "rev-parse", "--is-inside-work-tree", check=False).strip() != "true":
            fail(f"{self.repo} is not a git repository.")
        self.repo = Path(git(self.repo, "rev-parse", "--show-toplevel").strip())
        self.dir = self.repo / ".barton"

    def init(self) -> None:
        (self.dir / "runs").mkdir(parents=True, exist_ok=True)
        # Keep .barton out of the user's `git status` without touching .gitignore.
        exclude = Path(git(self.repo, "rev-parse", "--git-path", "info/exclude").strip())
        if not exclude.is_absolute():
            exclude = self.repo / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        text = exclude.read_text() if exclude.exists() else ""
        if ".barton/" not in text.split():
            exclude.write_text(text + ("" if text.endswith("\n") or not text else "\n") + ".barton/\n")

    @property
    def config_path(self) -> Path:
        return self.dir / "config.json"

    def config(self) -> dict:
        if not self.config_path.exists():
            return {"setup": "", "link": [], "copy": [], "tasks": []}
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def save_config(self, cfg: dict) -> None:
        self.init()
        self.config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                                    encoding="utf-8")

    def task(self, task_id: str) -> dict:
        for t in self.config().get("tasks", []):
            if t["id"] == task_id:
                return t
        fail(f"No task '{task_id}' in .barton/config.json.")

    def results(self) -> list[dict]:
        p = self.dir / "results.jsonl"
        if not p.exists():
            return []
        rows = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def append_result(self, row: dict) -> None:
        self.init()
        with (self.dir / "results.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- check

def cmd_check(args) -> None:
    """What is installed and logged in, as JSON for the test leader."""
    info: dict = {"barton": VERSION, "python": sys.version.split()[0],
                  "platform": sys.platform, "git": None, "claude": None, "codex": None}
    g = which("git")
    if g:
        info["git"] = subprocess.run([g, "--version"], capture_output=True,
                                     text=True).stdout.strip()
    c = which("claude")
    if c:
        v = subprocess.run([c, "--version"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL).stdout.strip()
        st = subprocess.run([c, "auth", "status"], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL)
        try:
            s = json.loads(st.stdout)
            info["claude"] = {"version": v, "logged_in": bool(s.get("loggedIn")),
                              "auth": s.get("authMethod")}
        except json.JSONDecodeError:
            info["claude"] = {"version": v, "logged_in": None}
    x = which("codex")
    if x:
        v = subprocess.run([x, "--version"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL).stdout.strip()
        st = subprocess.run([x, "login", "status"], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL)
        text = (st.stdout + st.stderr).strip()
        info["codex"] = {"version": v, "logged_in": st.returncode == 0 and "Logged in" in text,
                         "auth": text.splitlines()[0] if text else None}
    out(info)


# ---------------------------------------------------------------- mine

def is_test(path: str) -> bool:
    return bool(TEST_PATH.search(path.replace("\\", "/")))


def is_code(path: str) -> bool:
    return Path(path).suffix.lower() in CODE_EXT


def cmd_mine(args) -> None:
    """List commits that could become tasks, smallest self-contained first."""
    b = Barton(Path(args.repo))
    log = git(b.repo, "log", "--first-parent", "--no-merges", f"-n{args.limit}",
              "--numstat", "--format=@@%H%x09%aI%x09%s")
    commits, cur = [], None
    for line in log.splitlines():
        if line.startswith("@@"):
            sha, date, subject = line[2:].split("\t", 2)
            cur = {"sha": sha, "date": date, "subject": subject, "files": []}
            commits.append(cur)
        elif line.strip() and cur is not None:
            a, d, path = line.split("\t", 2)
            n = (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
            cur["files"].append((path, n))
    cands = []
    for c in commits:
        code = [p for p, _ in c["files"] if is_code(p) and not is_test(p)]
        tests = [p for p, _ in c["files"] if is_test(p)]
        lines = sum(n for _, n in c["files"])
        if not code or len(c["files"]) > args.max_files or lines > args.max_lines:
            continue
        if not git(b.repo, "rev-parse", "--verify", "-q", c["sha"] + "^", check=False).strip():
            continue  # root commit: no "before" state
        body = git(b.repo, "log", "-1", "--format=%b", c["sha"]).strip()
        cands.append({"sha": c["sha"][:12], "date": c["date"], "subject": c["subject"],
                      "body": body[:600], "code_files": code, "test_files": tests,
                      "lines_changed": lines, "has_tests": bool(tests)})
    cands.sort(key=lambda c: (not c["has_tests"], c["lines_changed"]))
    out({"repo": str(b.repo), "commits_scanned": len(commits),
         "candidates": cands[: args.show]})


# ---------------------------------------------------------------- task folders

def extract(repo: Path, sha: str, dest: Path) -> None:
    """Write the tree at `sha` into dest: files only, no history."""
    data = git(repo, "archive", "--format=tar", sha, binary=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for m in tar.getmembers():
            target = (dest / m.name).resolve()
            if not str(target).startswith(str(dest.resolve())):
                fail(f"Unsafe path in archive: {m.name}")
            if m.issym() or m.islnk():
                continue
            tar.extract(m, dest)


def link_dir(src: Path, dst: Path) -> None:
    if dst.exists() or not src.exists():
        return
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                       capture_output=True, stdin=subprocess.DEVNULL)
    else:
        os.symlink(src, dst, target_is_directory=True)


def make_folder(b: Barton, sha: str, name: str, files: dict[str, bytes | None] | None = None,
                second_commit: str | None = None) -> Path:
    """A fresh git repo holding the tree at `sha` as its only commit.

    `files` (path -> content, None = delete) is applied as a second commit when
    `second_commit` names it; this is how a review task gets its change."""
    cfg = b.config()
    dest = WORK_ROOT / name
    if dest.exists():
        remove_tree(dest)
    dest.mkdir(parents=True)
    extract(b.repo, sha, dest)
    ignore = ["# barton: linked and copied, never part of the work"]
    for rel in cfg.get("link", []):
        link_dir(b.repo / rel, dest / rel)
        ignore.append("/" + rel.strip("/"))
    for rel in cfg.get("copy", []):
        if (b.repo / rel).is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(b.repo / rel, dest / rel)
            ignore.append("/" + rel)
    git(dest, "init", "-q", "-b", "main")
    (dest / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (dest / ".git" / "info" / "exclude").write_text("\n".join(ignore) + "\n")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "--allow-empty", "-m", "start")
    if cfg.get("setup"):
        code, so, se, _, to = run_cmd(cfg["setup"], dest, CHECK_TIMEOUT, shell=True)
        if code != 0:
            fail(f"Setup command failed in {dest} (exit {code}): {(se or so)[-1500:]}")
        # Setup output that is not git-ignored is committed so it stays out of diffs.
        git(dest, "add", "-A", check=False)
        git(dest, "commit", "-q", "-m", "setup", check=False)
    if files is not None:
        for rel, content in files.items():
            p = dest / rel
            if content is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(content)
        git(dest, "add", "-A")
        git(dest, "commit", "-q", "--allow-empty", "-m", second_commit or "change")
    return dest


def remove_tree(p: Path) -> None:
    """Delete a task folder without following linked folders into the user's repo."""
    def onerror(func, path, _exc):
        os.chmod(path, 0o700)
        func(path)
    if os.name == "nt":  # rd /s removes junctions themselves, not their targets
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(p)], capture_output=True)
    if p.exists():
        shutil.rmtree(p, onerror=onerror)


def hidden_files(b: Barton, task: dict) -> dict[str, bytes | None]:
    """The check's files, as they were in the reference commit."""
    out_files: dict[str, bytes | None] = {}
    check = task.get("check", {})
    for rel in check.get("files", []):
        p = subprocess.run(["git", "-C", str(b.repo), "show", f"{task['reference']}:{rel}"],
                           capture_output=True, stdin=subprocess.DEVNULL)
        out_files[rel] = p.stdout if p.returncode == 0 else None
    # A check the test leader wrote itself, for projects without tests.
    for rel, text in check.get("write", {}).items():
        out_files[rel] = text.encode("utf-8")
    return out_files


def run_check(b: Barton, task: dict, folder: Path) -> dict:
    """Apply the hidden check files and run the check command."""
    for rel, content in hidden_files(b, task).items():
        p = folder / rel
        if content is None:
            if p.exists():
                p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
    code, so, se, secs, to = run_cmd(task["check"]["command"], folder, CHECK_TIMEOUT, shell=True,
                                     env=task["check"].get("env"))
    return {"passed": code == 0 and not to, "exit": code, "timed_out": to,
            "seconds": round(secs, 1), "tail": (so + se)[-1200:]}


# ---------------------------------------------------------------- tasks

def cmd_task_add(args) -> None:
    """Add or replace a task from a JSON file written by the test leader."""
    b = Barton(Path(args.repo))
    task = json.loads(Path(args.file).read_text(encoding="utf-8"))
    for key in ("id", "kind", "base", "prompt"):
        if not task.get(key):
            fail(f"Task needs '{key}'.")
    if task["kind"] not in ("write", "review"):
        fail("kind must be 'write' or 'review'.")
    task["base"] = git(b.repo, "rev-parse", task["base"]).strip()
    if task.get("reference"):
        task["reference"] = git(b.repo, "rev-parse", task["reference"]).strip()
    if task.get("check") and not task.get("reference"):
        fail("A task with a check needs 'reference' (the commit whose files the check uses).")
    task.setdefault("source", "history" if task.get("reference") else "written")
    save_task(b, task)
    out({"added": task["id"]})


def save_task(b: Barton, task: dict) -> None:
    """Tasks are frozen once a model has run them, so results stay comparable."""
    if any(r["task"] == task["id"] for r in b.results()):
        fail(f"Task '{task['id']}' already has results and cannot change. Use a new id.")
    cfg = b.config()
    cfg["tasks"] = [t for t in cfg.get("tasks", []) if t["id"] != task["id"]] + [task]
    b.save_config(cfg)


def cmd_config(args) -> None:
    """Project settings: how to prepare a task folder so the code can run."""
    b = Barton(Path(args.repo))
    cfg = b.config()
    if args.setup is not None:
        cfg["setup"] = args.setup
    if args.link is not None:
        cfg["link"] = [x for x in args.link if x]
    if args.copy is not None:
        cfg["copy"] = [x for x in args.copy if x]
    if args.public is not None:
        cfg["public"] = args.public == "yes"
    b.save_config(cfg)
    out({k: cfg.get(k) for k in ("setup", "link", "copy", "public")})


def cmd_task_remove(args) -> None:
    b = Barton(Path(args.repo))
    cfg = b.config()
    before = len(cfg.get("tasks", []))
    cfg["tasks"] = [t for t in cfg.get("tasks", []) if t["id"] != args.task]
    b.save_config(cfg)
    out({"removed": args.task if len(cfg["tasks"]) < before else None, "tasks": len(cfg["tasks"])})


def cmd_validate(args) -> None:
    """A check must fail before the change and pass, twice, with it."""
    b = Barton(Path(args.repo))
    task = b.task(args.task)
    if not task.get("check"):
        fail("This task has no check; it is judged blind instead.")
    before = run_check(b, task, make_folder(b, task["base"], f"v-{task['id']}-before"))
    after_dir = make_folder(b, task["reference"], f"v-{task['id']}-after")
    after1 = run_check(b, task, after_dir)
    after2 = run_check(b, task, after_dir)
    ok = (not before["passed"]) and after1["passed"] and after2["passed"]
    cfg = b.config()
    for t in cfg["tasks"]:
        if t["id"] == task["id"]:
            t["validated"] = {"ok": ok, "at": now()}
    b.save_config(cfg)
    for d in (f"v-{task['id']}-before", f"v-{task['id']}-after"):
        remove_tree(WORK_ROOT / d)
    out({"task": task["id"], "valid": ok, "before": before, "after": [after1, after2]})


# ---------------------------------------------------------------- planted bugs

MUTATIONS = [
    (r"===", "!=="), (r"!==", "==="), (r"==", "!="), (r"!=", "=="),
    (r"<=", "<"), (r">=", ">"), (r"(?<![<=!-])<(?![<=])", "<="), (r"(?<![>=-])>(?![>=])", ">="),
    (r"&&", "||"), (r"\|\|", "&&"), (r"\band\b", "or"), (r"\bor\b", "and"),
    (r"\btrue\b", "false"), (r"\bfalse\b", "true"), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
    (r"\+ 1\b", "- 1"), (r"- 1\b", "+ 1"),
]
COMMENT = re.compile(r"^\s*(#|//|/\*|\*|<!--)")


def cmd_plant(args) -> None:
    """Make a review task: a real commit with one mechanical bug planted in it."""
    b = Barton(Path(args.repo))
    if any(r["task"] == args.id for r in b.results()):
        fail(f"Task '{args.id}' already has results and cannot change. Use a new id.")
    sha = git(b.repo, "rev-parse", args.commit.strip()).strip()
    base = git(b.repo, "rev-parse", sha + "^").strip()
    diff = git(b.repo, "diff", "-U0", base, sha)
    spots, path, line_no = [], None, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            p = line[4:]
            path = p[2:] if p.startswith("b/") else None
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            line_no = int(m.group(1)) if m else 0
        elif line.startswith("+") and path and is_code(path) and not is_test(path):
            text = line[1:]
            # Code-like lines only: prose in docstrings has no brackets or assignments.
            if (text.strip() and not COMMENT.match(text) and len(text) < 300
                    and re.search(r"[=(){}\[\];]|\breturn\b|\bif\b|\bwhile\b", text)):
                for pat, rep in MUTATIONS:
                    for m in re.finditer(pat, text):
                        before = text[: m.start()]
                        if before.count('"') % 2 == 1 or before.count("'") % 2 == 1:
                            continue  # inside a string literal
                        spots.append((path, line_no, text, pat, rep, m.start(), m.end()))
            line_no += 1
    if not spots:
        fail("No line in this commit's added code can take a planted bug. Pick another commit.")
    rng = random.Random(args.seed if args.seed is not None else sha)
    rng.shuffle(spots)
    prompt = args.prompt or "Review the latest commit in this repository (`git show HEAD`) for bugs."
    tried = []
    for path, line_no, text, pat, rep, s, e in spots[: args.tries]:
        new_text = text[:s] + rep + text[e:]
        task = {
            "id": args.id, "kind": "review", "base": base, "reference": sha, "source": "planted",
            "prompt": prompt,
            "plant": {"file": path, "line": line_no, "before": text.strip(),
                      "after": new_text.strip(), "operator": f"{pat} -> {rep}"},
        }
        if not args.check:
            break
        # With the project's tests: keep only a bug they catch, so it is surely a real bug.
        task["plant"]["check"] = args.check
        name = f"p-{args.id}"
        folder = make_folder(b, base, name, review_files(b, task), "change")
        code, so, se, _, to = run_cmd(args.check, folder, CHECK_TIMEOUT, shell=True)
        remove_tree(folder)
        tried.append({"line": line_no, "after": new_text.strip(), "tests_failed": code != 0})
        if code != 0:
            task["plant"]["caught_by_tests"] = True
            break
    else:
        fail(f"None of the {len(tried)} planted bugs tried made the tests fail, so they may be "
             "harmless. Pick another commit.")
    save_task(b, task)
    out({"added": task["id"], **task["plant"], "tried": len(tried) or 1})


def review_files(b: Barton, task: dict) -> dict[str, bytes | None]:
    files: dict[str, bytes | None] = {}
    for rel in git(b.repo, "diff", "--name-only", task["base"], task["reference"]).splitlines():
        p = subprocess.run(["git", "-C", str(b.repo), "show", f"{task['reference']}:{rel}"],
                           capture_output=True, stdin=subprocess.DEVNULL)
        files[rel] = p.stdout if p.returncode == 0 else None
    pl = task["plant"]
    lines = files[pl["file"]].decode("utf-8", "replace").split("\n")
    old = lines[pl["line"] - 1]
    indent = old[: len(old) - len(old.lstrip())]
    lines[pl["line"] - 1] = indent + pl["after"] + ("\r" if old.endswith("\r") else "")
    files[pl["file"]] = "\n".join(lines).encode("utf-8")
    return files


# ---------------------------------------------------------------- run

WRITE_RULES = ("\n\nWork only in the current folder. Make the change in the code; "
               "add or update tests if the project has them. You may run the project's "
               "commands. Do not use the internet. When you are done, stop.")
REVIEW_RULES = ("\n\nDo not change the project's code. Report only bugs you are confident "
                "about. Do not use the internet. As your last step, create the file "
                f"`{REVIEW_FILE}` in the current folder: a JSON list with one object per bug, "
                '{"file": "<path>", "line": <line number in the current file>, '
                '"problem": "<one sentence>"}, or [] if you found none. This file is how your '
                "review is collected; a review given only in your reply may be lost.")


def harness_cmd(harness: str, model: str, effort: str | None, prompt: str, folder: Path):
    if harness == "claude":
        cmd = resolve_cli("claude") + [
            "-p", prompt, "--model", model, "--output-format", "json",
            "--permission-mode", "dontAsk",
            "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash,TodoWrite",
            "--disallowedTools", "WebFetch,WebSearch",
            "--setting-sources", "project,local", "--strict-mcp-config",
            "--no-session-persistence"]
        if effort:
            cmd += ["--effort", effort]
        return cmd
    if harness == "codex":
        cmd = resolve_cli("codex") + [
            "exec", "-m", model, "-s", "workspace-write", "-C", str(folder),
            "--skip-git-repo-check", "--json", "--ephemeral", "--ignore-user-config",
            "-c", 'web_search="disabled"',
            "--disable", "browser_use", "--disable", "computer_use", "--disable", "apps",
            "-c", 'approval_policy="never"']
        if os.name == "nt":  # without it, workspace-write rejects every command on Windows
            cmd += ["-c", 'windows.sandbox="unelevated"']
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        return cmd + [prompt]
    fail(f"Unknown harness '{harness}'. Use 'claude' or 'codex'.")


def parse_usage(harness: str, stdout: str) -> dict:
    u = {"input": 0, "cached": 0, "output": 0, "cost_usd": None, "error": None, "model_seen": None}
    if harness == "claude":
        try:
            j = json.loads(stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            u["error"] = "no JSON result from claude"
            return u
        us = j.get("usage") or {}
        u["input"] = int(us.get("input_tokens", 0)) + int(us.get("cache_creation_input_tokens", 0))
        u["cached"] = int(us.get("cache_read_input_tokens", 0))
        u["output"] = int(us.get("output_tokens", 0))
        u["cost_usd"] = j.get("total_cost_usd")
        u["final"] = str(j.get("result") or "")
        mu = j.get("modelUsage") or {}
        u["model_seen"] = ",".join(mu.keys()) or None
        if j.get("is_error") or j.get("subtype") not in (None, "success"):
            u["error"] = str(j.get("result") or j.get("subtype") or "error")[:300]
        return u
    saw_turn, last_error = False, None
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "turn.completed":
            saw_turn = True
            us = ev.get("usage") or {}
            cached = int(us.get("cached_input_tokens", 0))
            u["input"] += int(us.get("input_tokens", 0)) - cached
            u["cached"] += cached
            u["output"] += int(us.get("output_tokens", 0))
        elif ev.get("type") in ("turn.failed", "error"):
            last_error = str(ev.get("error") or ev.get("message") or ev)[:300]
        elif ev.get("type") == "item.completed":
            it = ev.get("item") or {}
            if it.get("type") == "command_execution" and it.get("exit_code") is not None:
                u["commands_ran"] = u.get("commands_ran", 0) + 1
            elif it.get("type") == "agent_message":
                u["final"] = (u.get("final", "") + "\n\n" + str(it.get("text") or "")).strip()
    if not saw_turn:  # a completed turn after a transient error is a real run
        u["error"] = last_error or "codex finished no turn"
    return u


def estimate_cost(model: str, u: dict) -> float | None:
    try:
        prices = json.loads(PRICES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    p = prices.get(model)
    if not p:
        return None
    return round((u["input"] * p["input"] + u["cached"] * p.get("cached", p["input"])
                  + u["output"] * p["output"]) / 1e6, 4)


def cmd_run(args) -> None:
    """Run one model on one task, check it, and record the result."""
    b = Barton(Path(args.repo))
    task = b.task(args.task)
    attempt = args.attempt
    done = [r for r in b.results() if r["task"] == task["id"] and r["model"] == args.model
            and r.get("effort") == args.effort and r["attempt"] == attempt
            and r["harness"] == args.harness and r["outcome"] != "error"]
    if done and not args.again:
        out({"skipped": "already recorded", "result": done[-1]})
        return
    run_id = f"{task['id']}-{re.sub(r'[^A-Za-z0-9.]+', '-', args.model)}-{args.effort or 'default'}-{attempt}"
    # The folder name shows up in the model's output, so it must not name the model.
    folder_name = f"{task['id']}-{os.urandom(4).hex()}"
    if task["kind"] == "review":
        folder = make_folder(b, task["base"], folder_name, review_files(b, task),
                             "change under review")
        prompt = task["prompt"] + REVIEW_RULES
    else:
        folder = make_folder(b, task["base"], folder_name)
        prompt = task["prompt"] + WRITE_RULES
    start_head = git(folder, "rev-parse", "HEAD").strip()  # the model may commit on its own
    cmd = harness_cmd(args.harness, args.model, args.effort, prompt, folder)
    code, so, se, secs, to = run_cmd(cmd, folder, args.timeout)
    u = parse_usage(args.harness, so)
    if to:
        u["error"] = u["error"] or f"timed out after {args.timeout} s"
    if (args.harness == "codex" and "blocked by policy" in se
            and not u.get("commands_ran")):
        # Not one command could run: an environment fault, not a model result.
        u["error"] = u["error"] or "the sandbox blocked every command the model tried"
    run_dir = b.dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "output.txt").write_text(so[-200000:] + "\n--- stderr ---\n" + se[-20000:],
                                        encoding="utf-8")
    row = {"v": 1, "task": task["id"], "kind": task["kind"], "source": task.get("source"),
           "model": args.model, "harness": args.harness, "harness_version": cli_version(args.harness),
           "effort": args.effort, "attempt": attempt, "run": run_id, "at": now(),
           "seconds": round(secs, 1), "tokens": {k: u[k] for k in ("input", "cached", "output")},
           "model_seen": u["model_seen"]}
    cost = u["cost_usd"]
    row["cost_usd"], row["cost_kind"] = ((round(cost, 4), "reported") if cost is not None
                                         else (estimate_cost(args.model, u), "estimated"))
    if row["cost_usd"] is None:
        row["cost_kind"] = "unknown"
    if u["error"]:
        row.update(outcome="error", method=None, error=u["error"])
        b.append_result(row)
        out({"result": row, "folder": str(folder),
             "note": "Errors are not scores. Fix the cause (login, model name, limits) and run again."})
        return
    if task["kind"] == "write":
        git(folder, "add", "-A")
        patch = git(folder, "diff", "--cached", start_head)
        (run_dir / "diff.patch").write_text(patch, encoding="utf-8")
        row["lines_changed"] = sum(1 for l in patch.splitlines()
                                   if l[:1] in "+-" and l[:3] not in ("+++", "---"))
        if task.get("check"):
            chk = run_check(b, task, folder)
            (run_dir / "check.txt").write_text(json.dumps(chk, indent=2), encoding="utf-8")
            row.update(outcome="solved" if chk["passed"] else "failed",
                       method=task["check"].get("method", "tests"))
        elif not patch.strip():
            row.update(outcome="failed", method="no change")
        else:
            row.update(outcome="pending", method="judge")
    else:
        row["method"] = "planted"
        row.update(grade_review(task, folder, run_dir, u.get("final", "")))
    b.append_result(row)
    if not args.keep:
        remove_tree(folder)
    out({"result": row})


def grade_review(task: dict, folder: Path, run_dir: Path, final: str = "") -> dict:
    """Match the review file against the answer key; without a file, a judge decides."""
    p = folder / REVIEW_FILE
    try:
        findings = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(findings, list):
            raise ValueError
    except (OSError, ValueError):
        # Models sometimes answer only in their reply. Reading free text for a match
        # is a judgment, so it goes to the blind judge rather than to a pattern.
        (run_dir / "final.txt").write_text(final, encoding="utf-8")
        return {"outcome": "pending" if final.strip() else "missed", "method": "judge",
                "note": f"no valid {REVIEW_FILE}"}
    (run_dir / "review.json").write_text(json.dumps(findings, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
    pl = task["plant"]
    hit = False
    for f in findings:
        if not isinstance(f, dict):
            continue
        fp = str(f.get("file", "")).replace("\\", "/").lstrip("./")
        try:
            ln = int(f.get("line", 0))
        except (TypeError, ValueError):
            ln = 0
        if fp and (fp.endswith(pl["file"]) or pl["file"].endswith(fp)):
            if abs(ln - pl["line"]) <= 3:
                hit = True
                f["planted"] = True
    other = sum(1 for f in findings if isinstance(f, dict) and not f.get("planted"))
    return {"outcome": "found" if hit else "missed", "findings": len(findings),
            "other_findings": other}


_versions: dict[str, str] = {}


def cli_version(harness: str) -> str | None:
    if harness not in _versions:
        exe = which(harness)
        _versions[harness] = (subprocess.run([exe, "--version"], capture_output=True, text=True,
                                             stdin=subprocess.DEVNULL).stdout.strip()
                              if exe else None)
    return _versions[harness]


# ---------------------------------------------------------------- blind judging

def cmd_blind(args) -> None:
    """Write an anonymous bundle of every pending solution to one task."""
    b = Barton(Path(args.repo))
    task = b.task(args.task)
    pend = [r for r in b.results() if r["task"] == task["id"] and r["outcome"] == "pending"]
    latest = {r["run"]: r for r in pend}
    runs = list(latest.values())
    if not runs:
        fail("No solutions are waiting for a blind judgment on this task.")
    random.Random().shuffle(runs)
    labels = {}
    if task["kind"] == "review":
        pl = task["plant"]
        parts = ["# Review task\n\nSeveral reviewers looked at the same commit. One bug was "
                 "planted in it on purpose. For each review, decide only whether it identifies "
                 "this planted bug (the right place and the right problem, in any words).\n",
                 f"# The planted bug (answer key)\n\nFile `{pl['file']}`, line {pl['line']}: "
                 f"`{pl['before']}` was changed to `{pl['after']}`.\n"]
    else:
        parts = [f"# Task\n\n{task['prompt']}\n"]
        if task.get("reference"):
            ref = git(b.repo, "diff", task["base"], task["reference"])
            parts.append("# How the project's own author did it (one valid way, not the only "
                         f"one)\n\n```diff\n{ref[:60000]}\n```\n")
    for i, r in enumerate(runs):
        label = chr(ord("A") + i)
        labels[label] = r["run"]
        if task["kind"] == "review":
            text = (b.dir / "runs" / r["run"] / "final.txt").read_text(encoding="utf-8")
            parts.append(f"# Review {label}\n\n{anonymize(text, runs)[:30000]}\n")
        else:
            patch = (b.dir / "runs" / r["run"] / "diff.patch").read_text(encoding="utf-8")
            parts.append(f"# Solution {label}\n\n```diff\n{anonymize(patch, runs)[:60000]}\n```\n")
    bundle = b.dir / "blind" / f"{task['id']}.md"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_text("\n".join(parts), encoding="utf-8")
    (b.dir / "blind" / f"{task['id']}.labels.json").write_text(json.dumps(labels), encoding="utf-8")
    out({"bundle": str(bundle), "solutions": len(labels)})


def anonymize(text: str, runs: list[dict]) -> str:
    """Remove task-folder paths and model names, which would unblind the judge."""
    text = re.sub(r"[A-Za-z]?:?[/\\]?[^\s()\[\]`'\"]*?\.barton[/\\]w[/\\][^/\\\s)\]]+[/\\]?",
                  "", text)
    for r in runs:
        for name in (r["run"], r["model"]):
            text = text.replace(name, "<model>")
    return text


def cmd_verdict(args) -> None:
    """Record a blind verdict for one labeled solution."""
    b = Barton(Path(args.repo))
    labels_p = b.dir / "blind" / f"{args.task}.labels.json"
    if not labels_p.exists():
        fail("Run `barton blind` for this task first.")
    run = json.loads(labels_p.read_text(encoding="utf-8")).get(args.label)
    if not run:
        fail(f"No solution labeled {args.label}.")
    rows = [r for r in b.results() if r["run"] == run]
    row = dict(rows[-1])
    allowed = ("found", "missed") if row["kind"] == "review" else ("solved", "failed")
    if args.outcome not in allowed:
        fail(f"For this task the outcome is {' or '.join(allowed)}.")
    row.update(outcome=args.outcome, method="judge", judge_model=args.judge_model,
               judge_note=args.note, at=now())
    b.append_result(row)
    out({"recorded": run, "outcome": args.outcome})


# ---------------------------------------------------------------- status and report

def latest_rows(rows: list[dict]) -> list[dict]:
    """The last record per run wins (a verdict replaces 'pending')."""
    last: dict[str, dict] = {}
    for r in rows:
        last[r["run"]] = r
    return list(last.values())


def current_rows(b: Barton) -> list[dict]:
    """Latest result per run, for tasks still in the set (a removed task drops out)."""
    ids = {t["id"] for t in b.config().get("tasks", [])}
    return [r for r in latest_rows(b.results()) if r["task"] in ids]


def cmd_status(args) -> None:
    b = Barton(Path(args.repo))
    cfg = b.config()
    rows = current_rows(b)
    summary: dict[str, dict] = {}
    for r in rows:
        key = f"{r['model']} ({r['harness']}, {r.get('effort') or 'default'})"
        s = summary.setdefault(key, {"solved_or_found": 0, "scored": 0, "pending": 0, "errors": 0,
                                     "seconds": 0.0, "cost_usd": 0.0})
        if r["outcome"] == "error":
            s["errors"] += 1
            continue
        if r["outcome"] == "pending":
            s["pending"] += 1
            continue
        s["scored"] += 1
        s["solved_or_found"] += r["outcome"] in ("solved", "found")
        s["seconds"] += r.get("seconds") or 0
        s["cost_usd"] += r.get("cost_usd") or 0
    out({"tasks": [{"id": t["id"], "kind": t["kind"], "source": t.get("source"),
                    "check": bool(t.get("check")), "valid": (t.get("validated") or {}).get("ok")}
                   for t in cfg.get("tasks", [])],
         "runs": len(rows), "by_model": summary})


def cmd_report(args) -> None:
    b = Barton(Path(args.repo))
    cfg = b.config()
    data = {"repo": b.repo.name, "generated": now(), "barton": VERSION,
            "public": cfg.get("public"), "tasks": [
                {k: t.get(k) for k in ("id", "kind", "source", "prompt")}
                | {"check": bool(t.get("check"))} for t in cfg.get("tasks", [])],
            "results": current_rows(b)}
    tpl = (HERE / "report.html").read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = tpl.replace("/*BARTON_DATA*/null", blob)
    dest = b.dir / "report.html"
    b.init()
    dest.write_text(page, encoding="utf-8")
    if not args.no_open:
        webbrowser.open(dest.as_uri())
    out({"report": str(dest)})


def cmd_clean(args) -> None:
    """Remove task folders left by interrupted or --keep runs."""
    if WORK_ROOT.exists():
        for d in WORK_ROOT.iterdir():
            remove_tree(d)
    out({"cleaned": str(WORK_ROOT)})


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> None:
    # Windows pipes default to the ANSI code page; commit messages are often not ANSI.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap =argparse.ArgumentParser(prog="barton", description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def p(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        if name not in ("check", "clean"):
            sp.add_argument("--repo", default=".", help="the repository under test")
        return sp

    p("check", cmd_check, "what is installed and logged in")
    sp = p("mine", cmd_mine, "list commits that could become tasks")
    sp.add_argument("--limit", type=int, default=400)
    sp.add_argument("--max-files", type=int, default=6)
    sp.add_argument("--max-lines", type=int, default=300)
    sp.add_argument("--show", type=int, default=40)
    sp = p("config", cmd_config, "project settings for task folders")
    sp.add_argument("--setup", help="command run once in each task folder, e.g. 'npm ci'")
    sp.add_argument("--link", nargs="*", help="folders linked from the repo, e.g. node_modules")
    sp.add_argument("--copy", nargs="*", help="untracked files copied in, e.g. .env")
    sp.add_argument("--public", choices=["yes", "no"], help="is the repo public?")
    sp = p("task-add", cmd_task_add, "add a task from a JSON file")
    sp.add_argument("file")
    sp = p("task-remove", cmd_task_remove, "remove a task")
    sp.add_argument("task")
    sp = p("validate", cmd_validate, "check fails before and passes after the change")
    sp.add_argument("task")
    sp = p("plant", cmd_plant, "make a review task with a planted bug")
    sp.add_argument("id")
    sp.add_argument("commit")
    sp.add_argument("--seed", type=int)
    sp.add_argument("--prompt")
    sp.add_argument("--check", help="test command; keep only a planted bug it catches")
    sp.add_argument("--tries", type=int, default=12)
    sp = p("run", cmd_run, "run one model on one task and record it")
    sp.add_argument("task")
    sp.add_argument("--harness", required=True, choices=["claude", "codex"])
    sp.add_argument("--model", required=True)
    sp.add_argument("--effort")
    sp.add_argument("--attempt", type=int, default=1)
    sp.add_argument("--timeout", type=int, default=RUN_TIMEOUT)
    sp.add_argument("--again", action="store_true", help="run even if already recorded")
    sp.add_argument("--keep", action="store_true", help="keep the task folder afterwards")
    sp = p("blind", cmd_blind, "anonymous bundle of solutions waiting for judgment")
    sp.add_argument("task")
    sp = p("verdict", cmd_verdict, "record a blind verdict")
    sp.add_argument("task")
    sp.add_argument("label")
    sp.add_argument("outcome", choices=["solved", "failed", "found", "missed"])
    sp.add_argument("--judge-model", required=True)
    sp.add_argument("--note", default="")
    p("status", cmd_status, "progress and scores so far")
    sp = p("report", cmd_report, "build and open the results page")
    sp.add_argument("--no-open", action="store_true")
    p("clean", cmd_clean, "remove leftover task folders")
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
