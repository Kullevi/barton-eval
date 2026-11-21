#!/usr/bin/env python3
"""Barton-eval: test a new AI model on your own codebase and share the result.

A Claude Code or Codex session is the test leader (see SKILL.md). This script
does the mechanical parts it asks for: checking the CLIs, mining task
candidates from git history, building clean task folders with no future
history, running a model headless, checking its work, recording results and
building the report page. Standard library only. See docs/DESIGN.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
SETUP_TIMEOUT = 3 * 60 * 60  # a monorepo build may first wait for the machine to have room
JOBS = 8  # runs of one model at the same time; the model's provider is the limit, not this computer

TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]+\.py$|_test\.(py|go|rb)$"
    r"|\.(test|spec)\.[jt]sx?$|Tests?\.(java|kt|cs|swift)$")
CODE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rb",
            ".rs", ".java", ".kt", ".cs", ".php", ".swift", ".c", ".cc", ".cpp",
            ".h", ".hpp", ".vue", ".svelte", ".dart", ".scala", ".ex", ".exs"}


# ---------------------------------------------------------------- helpers

def contain_crashes() -> bool:
    """On Windows, make every program Barton starts, and every program those start,
    crash with an error code instead of opening an error box nobody is there to close.

    The error mode alone is not enough: it is inherited, but PowerShell resets it for
    the programs it starts, so a test the agent runs from PowerShell could still open
    a box and hang until someone clicks it. A job object with "die on unhandled
    exception" holds for every process in it, and children join their parent's job."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.SetErrorMode(0x0001 | 0x0002 | 0x8000)  # no critical-error, crash or open-file boxes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("Basic", Basic), ("IoInfo", ctypes.c_ulonglong * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = k.CreateJobObjectW(None, None)
    if not job:
        return False
    info = Extended()
    # Die on unhandled exception; a program that must leave the job (an installer) still may.
    info.Basic.LimitFlags = 0x400 | 0x800
    ok = (k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
          and k.AssignProcessToJobObject(job, k.GetCurrentProcess()))
    return bool(ok)  # the handle stays open for the life of this process


def sp_run(cmd, **kw):
    """Every subprocess goes through here. On Windows it gets a hidden console, which
    its children (git, cmd, the model CLIs, test runners) share, so no terminal
    window opens; stdin is closed unless given."""
    if "input" not in kw:
        kw.setdefault("stdin", subprocess.DEVNULL)
    if os.name == "nt":
        kw["creationflags"] = kw.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kw)

_print_lock = threading.Lock()


def out(obj) -> None:
    with _print_lock:
        print(json.dumps(obj, indent=2, ensure_ascii=False), flush=True)


def progress(msg: str) -> None:
    """A line for whoever watches a slow step; stderr, so the JSON answer stays clean."""
    print(f"barton: {msg}", file=sys.stderr, flush=True)


def fail(msg: str, code: int = 1) -> None:
    out({"error": msg})
    sys.exit(code)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def git(repo: Path, *args: str, check: bool = True, binary: bool = False):
    p = sp_run(["git", "-C", str(repo), *GIT_ID, *args],
                       capture_output=True, stdin=subprocess.DEVNULL)
    if check and p.returncode != 0:
        fail(f"git {' '.join(args)} failed: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout if binary else p.stdout.decode("utf-8", errors="replace")


def which(name: str) -> str | None:
    return shutil.which(name)


def run_cmd(cmd: list[str] | str, cwd: Path, timeout: int, shell: bool = False,
            env: dict | None = None, stdin_text: str | None = None):
    """Run with stdin closed or holding stdin_text; return (code, stdout, stderr, seconds, timed_out).

    On a timeout the whole process tree is stopped, not just the program started, so
    nothing the model left running keeps a task folder locked or uses the machine."""
    t0 = time.time()
    full_env = {**os.environ, **env} if env else None
    kw = {"cwd": str(cwd), "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "shell": shell,
          "env": full_env, "stdin": subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True  # its own process group, so the group can be stopped
    p = subprocess.Popen(cmd, **kw)
    try:
        so, se = p.communicate(stdin_text.encode("utf-8") if stdin_text is not None else None,
                               timeout=timeout)
        to = False
    except subprocess.TimeoutExpired:
        stop_tree(p)
        so, se = p.communicate()
        to = True
    return ((-1 if to else p.returncode), so.decode("utf-8", "replace"),
            se.decode("utf-8", "replace"), time.time() - t0, to)


def stop_tree(p: subprocess.Popen) -> None:
    if os.name == "nt":
        sp_run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
    else:
        import signal
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        p.kill()
    except OSError:
        pass


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

    def prefs(self) -> list[dict]:
        p = self.dir / "prefs.jsonl"
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]

    def append_result(self, row: dict) -> None:
        self.init()
        # Runs finish at the same time, in this process and in others started for
        # other models, so each line is written whole under a lock.
        with os_lock(self.dir / "results.lock"):
            with (self.dir / "results.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")


class os_lock:
    """An exclusive lock between threads and processes, released by the operating
    system if its holder dies."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a+")
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_LOCK, 1)
                    return self
                except OSError:  # LK_LOCK gives up after ten seconds; keep waiting
                    pass
        import fcntl
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if os.name == "nt":
            import msvcrt
            self.f.seek(0)
            msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


# ---------------------------------------------------------------- check

def cmd_check(args) -> None:
    """What is installed and logged in, as JSON for the test leader."""
    info: dict = {"barton": VERSION, "python": sys.version.split()[0],
                  "platform": sys.platform, "git": None, "claude": None, "codex": None, "agy": None}
    g = which("git")
    if g:
        info["git"] = sp_run([g, "--version"], capture_output=True,
                                     text=True).stdout.strip()
    c = which("claude")
    if c:
        v = sp_run([c, "--version"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL).stdout.strip()
        st = sp_run([c, "auth", "status"], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL)
        try:
            s = json.loads(st.stdout)
            info["claude"] = {"version": v, "logged_in": bool(s.get("loggedIn")),
                              "auth": s.get("authMethod")}
        except json.JSONDecodeError:
            info["claude"] = {"version": v, "logged_in": None}
    x = which("codex")
    if x:
        v = sp_run([x, "--version"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL).stdout.strip()
        st = sp_run([x, "login", "status"], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL)
        text = (st.stdout + st.stderr).strip()
        info["codex"] = {"version": v, "logged_in": st.returncode == 0 and "Logged in" in text,
                         "auth": text.splitlines()[0] if text else None}
    a = which("agy")
    if a:
        v = sp_run([a, "--version"], capture_output=True, text=True).stdout.strip()
        try:  # listing models needs a signed-in account
            st = sp_run([a, "models"], capture_output=True, text=True, timeout=60)
            ok = st.returncode == 0 and "\t" in st.stdout
        except subprocess.TimeoutExpired:
            ok = None
        info["agy"] = {"version": v, "logged_in": ok}
    # Has the user used this CLI here? Only whether its settings folder exists; nothing in it is read.
    home = Path.home()
    # Antigravity's desktop app keeps .gemini/antigravity; only the CLI writes antigravity-cli.
    for name, folders in (("claude", [".claude"]), ("codex", [".codex"]),
                          ("agy", [".gemini/antigravity-cli"])):
        used = any((home / f).is_dir() for f in folders)
        info[name] = {**(info[name] or {"installed": False}), "used_here": used}
    out(info)


# ---------------------------------------------------------------- mine

def is_test(path: str) -> bool:
    return bool(TEST_PATH.search(path.replace("\\", "/")))


def is_code(path: str) -> bool:
    return Path(path).suffix.lower() in CODE_EXT


def size_of(code_lines: int) -> str:
    """Rough difficulty from the changed lines of non-test code."""
    return "small" if code_lines < 25 else "medium" if code_lines <= 150 else "large"


def cmd_mine(args) -> None:
    """List commits that could become tasks, grouped by size so a run can mix difficulty."""
    b = Barton(Path(args.repo))
    log = git(b.repo, "log", "--first-parent", "--diff-merges=first-parent", f"-n{args.limit}",
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
        code_lines = sum(n for p, n in c["files"] if p in code)
        cands.append({"sha": c["sha"][:12], "date": c["date"], "subject": c["subject"],
                      "body": body[:600], "code_files": code, "test_files": tests,
                      "code_lines": code_lines, "lines_changed": lines,
                      "has_tests": bool(tests)})
    groups: dict[str, list] = {"small": [], "medium": [], "large": []}
    for c in cands:  # git log order: newest first, so recent work comes first
        groups[size_of(c["code_lines"])].append(c)
    for g in groups.values():
        g.sort(key=lambda c: not c["has_tests"])  # stable: keeps newest first
    each = max(1, args.show // 3)
    out({"repo": str(b.repo), "commits_scanned": len(commits),
         "counts": {k: len(v) for k, v in groups.items()},
         "hint": "Choose hard tasks: mostly large and medium, few small. Small tasks rarely "
                 "separate strong models.",
         **{k: v[:each] for k, v in groups.items()}})


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
        sp_run(["cmd", "/c", "mklink", "/J", str(dst), str(src)],
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
    try:
        if cfg.get("setup"):
            copy_folder(cfg, prepared(b, cfg, sha), dest)
        else:
            dest.mkdir(parents=True)
            lay_out(b, cfg, sha, dest)
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
    except BaseException:
        remove_tree(dest)
        raise
    return dest


def lay_out(b: Barton, cfg: dict, sha: str, dest: Path) -> None:
    """The tree at `sha`, the linked and copied files, and a git repo whose only commit is it."""
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


def template_root() -> Path:
    return WORK_ROOT.parent / (WORK_ROOT.name + "-prepared")


def prepared(b: Barton, cfg: dict, sha: str) -> Path:
    """A folder for `sha` with the setup command already run, made once per commit.

    Setting up can mean installing and building a whole monorepo; doing it once per
    task, not once per task and model, is what makes large projects practical."""
    sha = git(b.repo, "rev-parse", sha + "^{commit}").strip()
    key = hashlib.sha256(json.dumps([str(b.repo.resolve()), cfg.get("setup"), cfg.get("link", []),
                                     cfg.get("copy", [])]).encode()).hexdigest()[:8]
    t = template_root() / f"{sha[:12]}-{key}"
    ready = t.with_name(t.name + ".ready")
    with os_lock(t.with_name(t.name + ".lock")):  # other runs of this task wait for it
        if ready.exists():
            return t
        if t.exists():
            remove_tree(t)
        t.mkdir(parents=True)
        try:
            lay_out(b, cfg, sha, t)
            code, so, se, _, to = run_cmd(cfg["setup"], t, SETUP_TIMEOUT, shell=True)
            if code != 0 or to:
                fail(f"Setup command failed for {sha[:12]} (exit {code}): {(se or so)[-1500:]}")
            # Setup output that is not git-ignored is committed so it stays out of diffs.
            git(t, "add", "-A", check=False)
            git(t, "commit", "-q", "-m", "setup", check=False)
        except BaseException:
            remove_tree(t)
            raise
        ready.write_text(now())
    return t


def copy_folder(cfg: dict, src: Path, dest: Path) -> None:
    """Copy a prepared folder, its git repo included; linked folders are linked again."""
    linked = {str(Path(r.strip("/"))) for r in cfg.get("link", [])}
    if os.name != "nt":  # cp keeps symlinks as links and is much faster on big trees
        dest.mkdir(parents=True)
        p = sp_run(["cp", "-a", f"{src}/.", str(dest)], capture_output=True, text=True)
        if p.returncode != 0:
            fail(f"Could not copy {src}: {p.stderr[-800:]}")
        return

    def skip(d, names):  # junctions would be copied as whole folders
        rel = os.path.relpath(d, src)
        return [n for n in names if str(Path(rel) / n if rel != "." else Path(n)) in linked]
    shutil.copytree(src, dest, symlinks=True, ignore=skip)
    for rel in linked:
        if (src / rel).exists():
            link_dir(Path(os.path.realpath(src / rel)), dest / rel)


def stop_inside(folder: Path) -> None:
    """Stop every process still working in a task folder. Codex starts each command in a
    session of its own, so stopping the CLI's process group leaves a type check or test
    run going; those are found by where they run (Linux /proc)."""
    proc = Path("/proc")
    if not proc.is_dir():
        return
    import signal
    inside = str(folder.resolve()) + os.sep
    for d in proc.iterdir():
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        try:
            where = [os.readlink(d / "cwd") + os.sep, os.readlink(d / "exe")]
        except OSError:
            continue
        if any(w.startswith(inside) for w in where):
            try:
                os.kill(int(d.name), signal.SIGKILL)
            except OSError:
                pass


def remove_tree(p: Path) -> None:
    """Delete a task folder without following linked folders into the user's repo."""
    stop_inside(p)
    def onerror(func, path, _exc):
        # Git's background maintenance can delete its own lock files mid-removal.
        try:
            os.chmod(path, 0o700)
            func(path)
        except FileNotFoundError:
            pass
    if os.name == "nt":  # rd /s removes junctions themselves, not their targets
        sp_run(["cmd", "/c", "rmdir", "/s", "/q", str(p)], capture_output=True)
    if p.exists():
        shutil.rmtree(p, onerror=onerror)


def hidden_files(b: Barton, task: dict) -> dict[str, bytes | None]:
    """The check's files, as they were in the reference commit."""
    out_files: dict[str, bytes | None] = {}
    check = task.get("check", {})
    for rel in check.get("files", []):
        p = sp_run(["git", "-C", str(b.repo), "show", f"{task['reference']}:{rel}"],
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
    code, so, se, secs, to = run_cmd(task["check"]["command"], folder, check_timeout(b), shell=True,
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
    task.setdefault("added", now())  # so the test leader can tell when the set grows old
    cfg = b.config()
    cfg["tasks"] = [t for t in cfg.get("tasks", []) if t["id"] != task["id"]] + [task]
    b.save_config(cfg)


def check_timeout(b: Barton) -> int:
    """Seconds a check may take. A project whose checks wait in a queue for a free build
    slot sets more, so waiting is never scored as the model's failure."""
    return int(b.config().get("check_timeout") or CHECK_TIMEOUT)


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
    if args.check_timeout is not None:
        cfg["check_timeout"] = args.check_timeout * 60
    if args.codex_sandbox is not None:
        cfg["codex_sandbox"] = args.codex_sandbox == "on"
    b.save_config(cfg)
    out({k: cfg.get(k) for k in ("setup", "link", "copy", "public", "check_timeout", "codex_sandbox")})


def cmd_task_remove(args) -> None:
    b = Barton(Path(args.repo))
    cfg = b.config()
    before = len(cfg.get("tasks", []))
    cfg["tasks"] = [t for t in cfg.get("tasks", []) if t["id"] != args.task]
    b.save_config(cfg)
    out({"removed": args.task if len(cfg["tasks"]) < before else None, "tasks": len(cfg["tasks"])})


def cmd_exclude(args) -> None:
    """Take a faulty task off the cards, with the reason shown, instead of deleting it.

    A task is faulty when its check asks for something its prompt leaves open or
    contradicts: every model then fails it for the task's fault, not its own."""
    b = Barton(Path(args.repo))
    b.task(args.task)
    cfg = b.config()
    for t in cfg["tasks"]:
        if t["id"] == args.task:
            if args.undo:
                t.pop("excluded", None)
            else:
                t["excluded"] = args.reason or fail("Give the reason with --reason.")
    b.save_config(cfg)
    out({"task": args.task, "excluded": None if args.undo else args.reason})


def cmd_fairness(args) -> None:
    """A bundle for a fresh judge: does the check ask for more than the prompt says?

    For a coding task every setting failed, and for any task before its first run."""
    b = Barton(Path(args.repo))
    task = b.task(args.task)
    if task["kind"] != "write" or not task.get("check"):
        fail("A fairness check is for coding tasks with a check.")
    files = task["check"].get("files", [])
    tests = git(b.repo, "diff", task["base"], task["reference"], "--", *files) if files else ""
    written = "\n".join(f"## {rel}\n\n```\n{text}\n```" for rel, text in task["check"].get("write", {}).items())
    parts = [f"# Task given to the model\n\n{task['prompt']}\n",
             "Below are the hidden tests that decide whether a solution passes, and how "
             "solutions failed them. Decide whether the task is fair: could a careful engineer "
             "who reads only the task write code that passes, or do the tests require something "
             "the task leaves open (an exact name, code, message, value or format with more than "
             "one reasonable reading) or contradicts? Answer `fair` or `unfair`, and in one "
             "sentence name what the tests require that the task does not say.\n",
             f"# Hidden tests (what the original change added)\n\n```diff\n{clipped(tests)}\n```\n"
             + (f"\n{written}\n" if written else "")]
    fails = [r for r in current_rows(b) if r["task"] == task["id"] and r["outcome"] == "failed"]
    for i, r in enumerate(fails[:3]):
        chk = b.dir / "runs" / r["run"] / "check.txt"
        if chk.exists():
            tail = json.loads(chk.read_text(encoding="utf-8")).get("tail", "")
            parts.append(f"# How solution {i + 1} failed\n\n```\n{clipped(tail)}\n```\n")
    dest = b.dir / "fairness" / f"{task['id']}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(parts), encoding="utf-8")
    out({"task": task["id"], "bundle": str(dest), "failed_runs": len(fails),
         "note": "Give it to a fresh judge. If unfair before any run, sharpen the prompt; "
                 "after runs the task is frozen, so `barton exclude` it with the judge's reason."})


def cmd_validate(args) -> None:
    """A check must fail before the change and pass, twice, with it."""
    b = Barton(Path(args.repo))
    task = b.task(args.task)
    if not task.get("check"):
        fail("This task has no check; it is judged blind instead.")
    try:
        progress(f"{task['id']}: checking without the change (it should fail)")
        before = run_check(b, task, make_folder(b, task["base"], f"v-{task['id']}-before"))
        after_dir = make_folder(b, task["base"], f"v-{task['id']}-after",
                                changed_files(b, task["base"], task["reference"]), "reference")
        progress(f"{task['id']}: checking with the change, 1 of 2")
        after1 = run_check(b, task, after_dir)
        progress(f"{task['id']}: checking with the change, 2 of 2")
        after2 = run_check(b, task, after_dir)
    finally:
        for d in (f"v-{task['id']}-before", f"v-{task['id']}-after"):
            remove_tree(WORK_ROOT / d)
    ok = (not before["passed"]) and after1["passed"] and after2["passed"]
    cfg = b.config()
    for t in cfg["tasks"]:
        if t["id"] == task["id"]:
            t["validated"] = {"ok": ok, "at": now()}
    b.save_config(cfg)
    out({"task": task["id"], "valid": ok, "before": before, "after": [after1, after2]})


# ---------------------------------------------------------------- planted bugs

MUTATIONS = [
    (r"===", "!=="), (r"!==", "==="), (r"==", "!="), (r"!=", "=="),
    (r"<=", "<"), (r">=", ">"), (r"(?<![<=!-])<(?![<=])", "<="), (r"(?<![>=-])>(?![>=])", ">="),
    (r"&&", "||"), (r"\|\|", "&&"), (r"\band\b", "or"), (r"\bor\b", "and"),
    (r"\btrue\b", "false"), (r"\bfalse\b", "true"), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
    (r"\+ 1\b", "- 1"), (r"- 1\b", "+ 1"),
    (r"\bis None\b", "is not None"), (r"\bis not None\b", "is None"),
    (r"\bnot in\b", "in"), (r"\bif not\b", "if"), (r"\[-1\]", "[0]"),
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
        if args.check:
            progress(f"{args.id}: trying a planted bug in {path}:{line_no}")
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
        env = dict(kv.split("=", 1) for kv in args.env or [])
        if env:
            task["plant"]["env"] = env
        name = f"p-{args.id}"
        folder = make_folder(b, base, name, review_files(b, task), "change")
        try:
            code, so, se, _, to = run_cmd(args.check, folder, check_timeout(b), shell=True, env=env)
        finally:
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


def changed_files(b: Barton, base: str, reference: str) -> dict[str, bytes | None]:
    """Every file the change from base to reference touches, as it is in reference."""
    files: dict[str, bytes | None] = {}
    for rel in git(b.repo, "diff", "--name-only", "--no-renames", base, reference).splitlines():
        p = sp_run(["git", "-C", str(b.repo), "show", f"{reference}:{rel}"],
                   capture_output=True, stdin=subprocess.DEVNULL)
        files[rel] = p.stdout if p.returncode == 0 else None
    return files


def review_files(b: Barton, task: dict) -> dict[str, bytes | None]:
    files = changed_files(b, task["base"], task["reference"])
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
REVIEW_RULES = ("\n\nDo not change any files. Report only bugs you are confident about. "
                "Do not use the internet. End your reply with the bugs as a JSON code block: "
                "```json followed by a list with one object per bug, "
                '{"file": "<path>", "line": <line number in the current file>, '
                '"problem": "<one sentence>"}, or [] if you found none.')


# The tools through which a model reaches the internet in Antigravity; agy has no switch
# to turn them off in print mode, so their use is counted and shown instead.
AGY_WEB = re.compile(r"^(search_web|read_url\w*|browser_\w+)$")


def prompt_on_stdin(harness: str) -> bool:
    """agy reads its prompt only from -p; it is a real executable, so the whole prompt
    survives as one argument. The other CLIs are .cmd shims on Windows."""
    return harness != "agy"


def harness_cmd(harness: str, model: str, effort: str | None, folder: Path, prompt: str = "",
                sandbox: bool = True):
    """The CLI command; for claude and codex the prompt goes on stdin, because Windows
    .cmd shims cut an argument at its first line break and drop every argument after it."""
    if harness == "claude":
        cmd = resolve_cli("claude") + [
            "-p", "--model", model, "--output-format", "json",
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
            "exec", "-m", model, "-s", "workspace-write" if sandbox else "danger-full-access",
            "-C", str(folder), "--skip-git-repo-check", "--json", "--ephemeral", "--ignore-user-config",
            "-c", 'web_search="disabled"',
            # The project's instruction files whole, as Claude reads them; codex's default
            # cuts them at 32 KB, and the user's own raised limit is in the ignored config.
            "-c", "project_doc_max_bytes=1048576",
            "--disable", "browser_use", "--disable", "computer_use", "--disable", "apps",
            "-c", 'approval_policy="never"']
        if os.name == "nt":  # without it, workspace-write rejects every command on Windows
            cmd += ["-c", 'windows.sandbox="unelevated"']
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        return cmd + ["-"]
    if harness == "agy":
        exe = resolve_cli("agy")
        if exe[0].lower().endswith((".cmd", ".bat")):
            fail("Barton needs agy.exe itself, not a .cmd wrapper, to pass the prompt whole.")
        # The model name is pinned: agy's default "Auto" may pick another model. agy's
        # model ids carry the thinking level (gemini-4-argon-medium); Barton records model
        # and level apart, as for the other CLIs, and joins them here.
        slug = model if not effort or model.endswith("-" + effort) else f"{model}-{effort}"
        return exe + ["-p", prompt, "--model", slug, "--output-format", "stream-json",
                      "--dangerously-skip-permissions", "--disable-slash-commands"]
    fail(f"Unknown harness '{harness}'. Use 'claude', 'codex' or 'agy'.")


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
    if harness == "agy":
        return parse_agy(stdout, u)
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


def parse_agy(stdout: str, u: dict) -> dict:
    """agy --output-format stream-json: an init event naming the resolved model, one
    step_update per step, and a result event with the totals."""
    result, web, denied, tools = None, 0, [], 0
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("event") == "init":
            u["model_seen"] = (ev.get("init") or {}).get("model")
        elif ev.get("event") == "step_update":
            st = ev.get("step_update") or {}
            if st.get("step_type") != "tool" or st.get("state") == "ACTIVE":
                continue
            name = str(st.get("tool_name") or "")
            if st.get("state") == "DONE":
                tools += 1
                web += bool(AGY_WEB.match(name))
            else:  # a refused or failed tool call
                denied.append(f"{name}: {st.get('state')}")
        elif ev.get("event") == "result":
            result = ev.get("result") or {}
    u["commands_ran"], u["web_used"] = tools, web
    if denied:
        u["tool_denials"] = denied[:20]
    if result is None:
        u["error"] = "agy gave no result"
        return u
    us = result.get("usage") or {}
    u["input"] = int(us.get("input_tokens", 0))
    u["cached"] = int(us.get("cache_read_tokens", 0))
    u["output"] = int(us.get("output_tokens", 0)) + int(us.get("thinking_tokens", 0))
    u["final"] = str(result.get("response") or "")
    if result.get("status") != "SUCCESS":
        u["error"] = str(result.get("error") or result.get("status") or "error")[:300]
    return u


def estimate_cost(model: str, u: dict, day: str | None = None) -> float | None:
    """What the tokens would cost at the vendor's list API price on `day` (default today).
    A model whose price changes lists its periods, each with the date it starts."""
    try:
        prices = json.loads(PRICES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    p = prices.get(model)
    if isinstance(p, list):
        day = day or dt.datetime.now(dt.timezone.utc).date().isoformat()
        p = ([x for x in p if x.get("from", "") <= day] or [None])[-1]
    if not p:
        return None
    return round((u["input"] * p["input"] + u["cached"] * p.get("cached", p["input"])
                  + u["output"] * p["output"]) / 1e6, 4)


def cmd_run(args) -> None:
    """Run one model on the given tasks (or `all`), several at once, and record each."""
    b = Barton(Path(args.repo))
    cfg = b.config()
    if args.tasks == ["all"]:
        ids = [t["id"] for t in cfg.get("tasks", []) if not t.get("excluded")]
    elif args.tasks == ["quick"]:
        ids = cfg.get("quick") or fail("No quick set yet. Run `barton quick` first.")
    else:
        ids = args.tasks
    tasks = [b.task(i) for i in dict.fromkeys(ids)]
    recorded = b.results()
    todo = []
    for task in tasks:
        done = [r for r in recorded if r["task"] == task["id"] and r["model"] == args.model
                and r.get("effort") == args.effort and r["attempt"] == args.attempt
                and r["harness"] == args.harness and r["outcome"] != "error"]
        if done and not args.again:
            out({"skipped": "already recorded", "result": done[-1]})
        else:
            todo.append(task)

    deadline = time.time() + args.budget * 60 if args.budget else None

    def one(task: dict) -> str:
        if deadline and time.time() > deadline:
            # Out of time: tasks not started yet are left for later, not scored.
            out({"task": task["id"], "skipped": f"the {args.budget}-minute budget was used up"})
            return "not started"
        try:
            return run_one(b, task, args)["outcome"]
        except BaseException as e:  # fail() exits; one broken task must not stop the rest
            msg = str(e) if not isinstance(e, SystemExit) else "see the error above"
            out({"task": task["id"], "error": f"the run could not start: {msg}"})
            return "error"

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        outcomes = list(pool.map(one, todo))
    try:
        WORK_ROOT.rmdir()  # only when every task folder is gone
    except OSError:
        pass
    if len(tasks) > 1:
        out({"ran": len(todo), "skipped": len(tasks) - len(todo),
             "outcomes": {o: outcomes.count(o) for o in sorted(set(outcomes))}})


def run_one(b: Barton, task: dict, args) -> dict:
    """Run one model on one task in a fresh folder, check it, record it, remove the folder."""
    attempt = args.attempt
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
    try:
        start_head = git(folder, "rev-parse", "HEAD").strip()  # the model may commit on its own
        cmd = harness_cmd(args.harness, args.model, args.effort, folder, prompt,
                          sandbox=b.config().get("codex_sandbox", True))
        # A Python that crashes while the model works prints where, instead of dying silently.
        code, so, se, secs, to = run_cmd(cmd, folder, args.timeout, env={"PYTHONFAULTHANDLER": "1"},
                                         stdin_text=prompt if prompt_on_stdin(args.harness) else None)
        u = parse_usage(args.harness, so)
        if args.harness == "codex" and not u.get("commands_ran"):
            # Not one command could run: an environment fault, not a model result.
            if "bwrap:" in so or "bwrap:" in se:
                u["error"] = u["error"] or (
                    "codex's sandbox cannot start on this computer (Linux here does not let it "
                    "create its own namespace), so no command could run. The task folder is a "
                    "throwaway copy; `barton config --codex-sandbox off` runs codex without it.")
            elif "blocked by policy" in se:
                u["error"] = u["error"] or "the sandbox blocked every command the model tried"
        if args.harness == "agy" and u.get("tool_denials") and not u.get("commands_ran"):
            # Headless agy refuses tools it may not use and still exits 0.
            u["error"] = u["error"] or "agy refused every tool call: " + "; ".join(u["tool_denials"][:3])
        run_dir = b.dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "output.txt").write_text(so[-200000:] + "\n--- stderr ---\n" + se[-20000:],
                                            encoding="utf-8")
        row = {"v": 1, "task": task["id"], "kind": task["kind"], "source": task.get("source"),
               "model": args.model, "harness": args.harness, "harness_version": cli_version(args.harness),
               "effort": args.effort, "attempt": attempt, "run": run_id, "at": now(),
               "seconds": round(secs, 1), "tokens": {k: u[k] for k in ("input", "cached", "output")},
               "model_seen": u["model_seen"]}
        for k in ("web_used", "tool_denials"):  # agy only: it cannot switch the web off
            if u.get(k):
                row[k] = u[k]
        cost = u["cost_usd"]
        row["cost_usd"], row["cost_kind"] = ((round(cost, 4), "reported") if cost is not None
                                             else (estimate_cost(args.model, u), "estimated"))
        if row["cost_usd"] is None:
            row["cost_kind"] = "unknown"
        if to:
            # Running out of time is the model's result, not a fault to retry: a task it
            # could not finish counts as not solved.
            row.update(outcome="failed" if task["kind"] == "write" else "missed",
                       method="timed out", note=f"stopped after {args.timeout // 60} minutes")
            b.append_result(row)
            out({"result": row})
            return row
        if u["error"]:
            row.update(outcome="error", method=None, error=u["error"])
            b.append_result(row)
            out({"result": row,
                 "note": "Errors are not scores. Fix the cause (login, model name, limits) and run again."})
            return row
        if task["kind"] == "write":
            git(folder, "add", "-A")
            patch = git(folder, "diff", "--cached", "--ignore-cr-at-eol", start_head)  # Windows tools rewrite line endings
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
        out({"result": row})
        return row
    finally:
        # Every run cleans up after itself, also after an error or an interruption.
        if not args.keep:
            remove_tree(folder)


CLIP = 300_000  # characters of one diff or review in a judge bundle


def clipped(text: str) -> str:
    if len(text) <= CLIP:
        return text
    return text[:CLIP] + f"\n[... clipped here: {len(text) - CLIP} more characters not shown]\n"


def review_list(final: str):
    """The last ```json block in the reply that holds a list, or None."""
    for block in reversed(re.findall(r"```json\s*\n(.*?)```", final, re.S)):
        try:
            v = json.loads(block)
        except ValueError:
            continue
        if isinstance(v, list):
            return v
    return None


def grade_review(task: dict, folder: Path, run_dir: Path, final: str = "") -> dict:
    """Match the reply's bug list against the answer key; without a list, a judge decides."""
    (run_dir / "final.txt").write_text(final, encoding="utf-8")
    findings = review_list(final)
    if findings is None:
        # Reading free text for a match is a judgment, so it goes to the blind judge
        # rather than to a pattern.
        return {"outcome": "pending" if final.strip() else "missed", "method": "judge",
                "note": "no JSON bug list in the reply"}
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
        _versions[harness] = (sp_run([exe, "--version"], capture_output=True, text=True,
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
                         f"one)\n\n```diff\n{clipped(ref)}\n```\n")
    for i, r in enumerate(runs):
        label = chr(ord("A") + i)
        labels[label] = r["run"]
        if task["kind"] == "review":
            text = (b.dir / "runs" / r["run"] / "final.txt").read_text(encoding="utf-8")
            parts.append(f"# Review {label}\n\n{clipped(anonymize(text, runs))}\n")
        else:
            patch = (b.dir / "runs" / r["run"] / "diff.patch").read_text(encoding="utf-8")
            parts.append(f"# Solution {label}\n\n```diff\n{clipped(anonymize(patch, runs))}\n```\n")
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


# ---------------------------------------------------------------- quality when both solved

def setting(r: dict) -> str:
    return f"{r['model']}|{r['harness']}|{r.get('effort') or 'default'}"


def cmd_pairs(args) -> None:
    """Blind A/B bundles for every pair of solved solutions from different settings.

    Each pair is written in both orders; a preference counts only when both
    orders agree, which cancels the judge's bias toward the first or second."""
    b = Barton(Path(args.repo))
    task = b.task(args.task)
    if task["kind"] != "write":
        fail("Quality comparison is for coding tasks.")
    solved = [r for r in current_rows(b) if r["task"] == task["id"] and r["outcome"] == "solved"]
    by_setting: dict[str, dict] = {}
    for r in sorted(solved, key=lambda r: r["attempt"]):
        by_setting.setdefault(setting(r), r)  # one solution per setting: its first attempt
    runs = list(by_setting.values())
    done = {(p["first"], p["second"]) for p in b.prefs() if p["task"] == task["id"]}
    pdir = b.dir / "pairs"
    pdir.mkdir(parents=True, exist_ok=True)
    labels = json.loads((pdir / "labels.json").read_text()) if (pdir / "labels.json").exists() else {}
    written = []
    ref = git(b.repo, "diff", task["base"], task["reference"]) if task.get("reference") else ""
    for i in range(len(runs)):
        for j in range(i + 1, len(runs)):
            for first, second in ((runs[i], runs[j]), (runs[j], runs[i])):
                if (first["run"], second["run"]) in done:
                    continue
                pid = f"{task['id']}-{os.urandom(3).hex()}"
                labels[pid] = {"task": task["id"], "first": first["run"], "second": second["run"]}
                parts = [f"# Task\n\n{task['prompt']}\n",
                         "Both solutions below pass the project's checks. Decide which is the "
                         "better change to merge: correct in cases the checks may miss, simpler, "
                         "easier to maintain, and in the style of the surrounding code. Size is "
                         "not a merit in itself. Answer `first`, `second` or `tie`.\n"]
                if ref:
                    parts.append("# The project author's own change (for context; not the "
                                 f"answer key)\n\n```diff\n{clipped(ref)}\n```\n")
                for title, r in (("First", first), ("Second", second)):
                    patch = (b.dir / "runs" / r["run"] / "diff.patch").read_text(encoding="utf-8")
                    parts.append(f"# {title} solution\n\n```diff\n"
                                 f"{clipped(anonymize(patch, runs))}\n```\n")
                (pdir / f"{pid}.md").write_text("\n".join(parts), encoding="utf-8")
                written.append({"pair": pid, "bundle": str(pdir / f"{pid}.md")})
    (pdir / "labels.json").write_text(json.dumps(labels, indent=1), encoding="utf-8")
    out({"task": task["id"], "settings": len(runs), "bundles": written,
         "note": "Judge each bundle separately and blind, then record `barton prefer`."})


def cmd_prefer(args) -> None:
    b = Barton(Path(args.repo))
    labels = json.loads((b.dir / "pairs" / "labels.json").read_text(encoding="utf-8"))
    pair = labels.get(args.pair)
    if not pair:
        fail(f"No pair '{args.pair}'.")
    b.init()
    with (b.dir / "prefs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({**pair, "pair": args.pair, "choice": args.choice,
                            "judge_model": args.judge_model, "note": args.note,
                            "at": now()}, ensure_ascii=False) + "\n")
    out({"recorded": args.pair, "choice": args.choice})


def judge_order(b: Barton) -> dict:
    """How often each pairwise judge chose the first, the second or neither.

    Both orders are asked, so a judge that mostly picks one position turns most of
    its answers into ties; a share far from even says the judge is weak here."""
    seen: dict[str, dict] = {}
    for p in b.prefs():
        c = seen.setdefault(p["judge_model"], {"first": 0, "second": 0, "tie": 0})
        c[p["choice"]] += 1
    for c in seen.values():
        picked = c["first"] + c["second"]
        c["second_share"] = round(c["second"] / picked, 2) if picked else None
        c["note"] = ("Picks one position most of the time, so most of its answers cancel "
                     "to ties; prefer a stronger judge." if picked >= 10
                     and abs(c["second_share"] - .5) > .2 else "")
    return seen


def quality(b: Barton) -> list[dict]:
    """Wins, ties and losses per setting from preferences that agree in both orders."""
    rows = {r["run"]: r for r in current_rows(b)}
    votes: dict[tuple, dict] = {}
    for p in b.prefs():
        if p["first"] not in rows or p["second"] not in rows:
            continue
        key = tuple(sorted((p["first"], p["second"])))
        winner = {"first": p["first"], "second": p["second"]}.get(p["choice"])
        votes.setdefault(key, {})[(p["first"], p["second"])] = winner
    out_rows = []
    for (x, y), v in votes.items():
        if len(v) < 2:
            continue  # one order only: not yet a result
        a, c = v.values()
        winner = a if a == c else None
        out_rows.append({"task": rows[x]["task"], "a": setting(rows[x]), "b": setting(rows[y]),
                         "winner": setting(rows[winner]) if winner else None})
    return out_rows


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
        if s["cost_usd"] is not None:  # one run without a cost makes the total unknown, not lower
            s["cost_usd"] = None if r.get("cost_usd") is None else s["cost_usd"] + r["cost_usd"]
    out({"tasks": [{"id": t["id"], "kind": t["kind"], "source": t.get("source"),
                    "check": bool(t.get("check") or (t.get("plant") or {}).get("check")), "valid": (t.get("validated") or {}).get("ok")}
                   | ({"excluded": t["excluded"]} if t.get("excluded") else {})
                   for t in cfg.get("tasks", [])],
         "runs": len(rows), "by_model": summary, "quick": cfg.get("quick", []),
         "judges": judge_order(b),
         "task_set": task_age(cfg, rows)})


def cmd_report(args) -> None:
    b = Barton(Path(args.repo))
    cfg = b.config()
    ids = set(cfg.get("quick") or []) if args.quick else {t["id"] for t in cfg.get("tasks", [])}
    if args.quick and not ids:
        fail("No quick set yet. Run `barton quick` first.")
    excluded = [{"id": t["id"], "reason": t["excluded"]} for t in cfg.get("tasks", [])
                if t["id"] in ids and t.get("excluded")]
    ids -= {e["id"] for e in excluded}
    rows = [r for r in current_rows(b) if r["task"] in ids]
    # Every model is compared on the same tasks: a task some model has no result for
    # (not run yet, or an error to fix) is left off the cards until it does.
    done: dict[str, set] = {}
    for r in rows:
        if r["outcome"] != "error":
            done.setdefault(setting(r), set()).add(r["task"])
    shared = set.intersection(*done.values()) if done else set()
    left_out = sorted({r["task"] for r in rows} - shared)
    tasks = [t for t in cfg.get("tasks", []) if t["id"] in shared]
    data = {"repo": b.repo.name, "generated": now(), "barton": VERSION,
            "public": cfg.get("public"), "quick": bool(args.quick), "left_out": left_out,
            "excluded": excluded,
            "tasks": [{k: t.get(k) for k in ("id", "kind", "source", "prompt")}
                      | {"check": bool(t.get("check"))} for t in tasks],
            "results": [r for r in rows if r["task"] in shared],
            "quality": [q for q in quality(b) if q.get("task") in shared],
            "quality_judges": sorted({p["judge_model"] for p in b.prefs()})}
    tpl = (HERE / "report.html").read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = tpl.replace("/*BARTON_DATA*/null", blob)
    dest = b.dir / "report.html"
    b.init()
    dest.write_text(page, encoding="utf-8")
    if not args.no_open:
        webbrowser.open(dest.as_uri())
    out({"report": str(dest), "tasks": len(tasks),
         **({"left_out": left_out, "note": "Left out because not every model has a result for "
             "them yet; run what is missing and make the report again."} if left_out else {})})


def cmd_quick(args) -> None:
    """Choose the quick set: a few tasks that ran fast and still told the models apart.

    The quick set is part of the full set, so a quick run and a later full run share
    their results instead of doing the work twice."""
    b = Barton(Path(args.repo))
    cfg = b.config()
    rows = [r for r in current_rows(b) if r["outcome"] not in ("error", "pending")]
    chosen = []
    for kind, want in (("write", args.coding), ("review", args.reviews)):
        scored = []
        for t in cfg.get("tasks", []):
            if t["kind"] != kind or (kind == "write" and not t.get("check")) or t.get("excluded"):
                continue
            rs = [r for r in rows if r["task"] == t["id"]]
            secs = sorted(r["seconds"] for r in rs) or [0]
            mixed = len({r["outcome"] for r in rs}) > 1  # some models made it and some did not
            # Tasks that told models apart first, then the fastest; untried tasks keep their order.
            scored.append((not mixed, secs[len(secs) // 2] if rs else float("inf"), t["id"]))
        chosen += [tid for _, _, tid in sorted(scored)[:want]]
    cfg["quick"] = chosen
    b.save_config(cfg)
    out({"quick": chosen, "note": "Tasks that told models apart come first, then the fastest. "
                                  "Run them with `barton run quick ...`."})


def task_age(cfg: dict, rows: list[dict]) -> dict:
    """When the task set was last added to, so a stale set can be refreshed. A task from
    before Barton recorded this counts from its first result."""
    first: dict[str, str] = {}
    for r in rows:
        if r.get("at"):
            first[r["task"]] = min(first.get(r["task"], r["at"]), r["at"])
    added = sorted(t.get("added") or first[t["id"]] for t in cfg.get("tasks", [])
                   if t.get("added") or t["id"] in first)
    if not added:
        return {}
    newest = dt.datetime.fromisoformat(added[-1])
    days = (dt.datetime.now(dt.timezone.utc) - newest).days
    return {"newest_task_added": added[-1], "days_since": days}


def cmd_clean(args) -> None:
    """Remove task folders left by killed or --keep runs, and the prepared folders."""
    for root in (WORK_ROOT, template_root()):
        if root.exists():
            for d in root.iterdir():
                remove_tree(d) if d.is_dir() else d.unlink()
            root.rmdir()
    out({"cleaned": [str(WORK_ROOT), str(template_root())]})


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> None:
    contain_crashes()
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
    sp.add_argument("--max-files", type=int, default=15)
    sp.add_argument("--max-lines", type=int, default=1000)
    sp.add_argument("--show", type=int, default=45)
    sp = p("config", cmd_config, "project settings for task folders")
    sp.add_argument("--setup", help="command run once per task commit, e.g. 'npm ci'; each run gets a copy")
    sp.add_argument("--link", nargs="*", help="folders linked from the repo, e.g. node_modules")
    sp.add_argument("--copy", nargs="*", help="untracked files copied in, e.g. .env")
    sp.add_argument("--public", choices=["yes", "no"], help="is the repo public?")
    sp.add_argument("--check-timeout", type=int, metavar="MINUTES",
                    help="how long one check may take (default 15), longer if checks queue for a build slot")
    sp.add_argument("--codex-sandbox", choices=["on", "off"],
                    help="off only where codex's sandbox cannot start; the task folder is a throwaway copy")
    sp = p("task-add", cmd_task_add, "add a task from a JSON file")
    sp.add_argument("file")
    sp = p("task-remove", cmd_task_remove, "remove a task")
    sp.add_argument("task")
    sp = p("exclude", cmd_exclude, "take a faulty task off the cards, with the reason shown")
    sp.add_argument("task")
    sp.add_argument("--reason", help="one sentence: what the check asks that the prompt does not say")
    sp.add_argument("--undo", action="store_true")
    sp = p("fairness", cmd_fairness, "bundle for a judge: does the check ask more than the prompt says")
    sp.add_argument("task")
    sp = p("validate", cmd_validate, "check fails before and passes after the change")
    sp.add_argument("task")
    sp = p("plant", cmd_plant, "make a review task with a planted bug")
    sp.add_argument("id")
    sp.add_argument("commit")
    sp.add_argument("--seed", type=int)
    sp.add_argument("--prompt")
    sp.add_argument("--check", help="test command; keep only a planted bug it catches")
    sp.add_argument("--env", action="append", metavar="NAME=VALUE",
                    help="environment for --check, such as PYTHONPATH=src (repeatable)")
    sp.add_argument("--tries", type=int, default=12)
    sp = p("run", cmd_run, "run one model on tasks, several at once, and record each")
    sp.add_argument("tasks", nargs="+", help="task ids, all, or quick")
    sp.add_argument("--jobs", type=int, default=JOBS, help="runs at the same time")
    sp.add_argument("--harness", required=True, choices=["claude", "codex", "agy"])
    sp.add_argument("--model", required=True)
    sp.add_argument("--effort")
    sp.add_argument("--attempt", type=int, default=1)
    sp.add_argument("--timeout", type=int, default=RUN_TIMEOUT)
    sp.add_argument("--again", action="store_true", help="run even if already recorded")
    sp.add_argument("--budget", type=int, metavar="MINUTES",
                    help="start no new task after this many minutes; what ran is kept")
    sp.add_argument("--keep", action="store_true", help="keep the task folder afterwards")
    sp = p("quick", cmd_quick, "choose the quick set: a few fast tasks that told models apart")
    sp.add_argument("--coding", type=int, default=8)
    sp.add_argument("--reviews", type=int, default=4)
    sp = p("blind", cmd_blind, "anonymous bundle of solutions waiting for judgment")
    sp.add_argument("task")
    sp = p("verdict", cmd_verdict, "record a blind verdict")
    sp.add_argument("task")
    sp.add_argument("label")
    sp.add_argument("outcome", choices=["solved", "failed", "found", "missed"])
    sp.add_argument("--judge-model", required=True)
    sp.add_argument("--note", default="")
    sp = p("pairs", cmd_pairs, "blind A/B bundles of solved solutions to one task")
    sp.add_argument("task")
    sp = p("prefer", cmd_prefer, "record which solution in a pair is better")
    sp.add_argument("pair")
    sp.add_argument("choice", choices=["first", "second", "tie"])
    sp.add_argument("--judge-model", required=True)
    sp.add_argument("--note", default="")
    p("status", cmd_status, "progress and scores so far")
    sp = p("report", cmd_report, "build and open the results page")
    sp.add_argument("--no-open", action="store_true")
    sp.add_argument("--quick", action="store_true", help="only the quick set, and say so on the cards")
    p("clean", cmd_clean, "remove leftover task folders")
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
