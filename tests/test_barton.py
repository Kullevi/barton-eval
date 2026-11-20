"""Tests for barton.py: task folders, mining, planting, grading and parsing.

Builds a small git repository in a temp folder; no model is called.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import barton  # noqa: E402


def sh(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *barton.GIT_ID, *args], check=True,
                          capture_output=True, text=True).stdout


def call(*argv: str) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        barton.main(list(argv))
    return json.loads(buf.getvalue())


class RepoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "proj"
        self.repo.mkdir()
        barton.WORK_ROOT = root / "work"
        sh(self.repo, "init", "-q", "-b", "main")
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-q", "-m", "start")
        (self.repo / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef clamp(x, lo, hi):\n"
            "    if x < lo:\n        return lo\n    if x > hi:\n        return hi\n    return x\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "__init__.py").write_text("")
        (self.repo / "tests" / "test_calc.py").write_text(
            "import unittest\nfrom calc import clamp\n\n\nclass T(unittest.TestCase):\n"
            "    def test_clamp(self):\n        self.assertEqual(clamp(5, 0, 3), 3)\n"
            "        self.assertEqual(clamp(-1, 0, 3), 0)\n")
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-q", "-m", "Add clamp")
        self.fix = sh(self.repo, "rev-parse", "HEAD").strip()
        (self.repo / "later.txt").write_text("the future\n")
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-q", "-m", "Later work")

    def tearDown(self):
        barton.remove_tree(barton.WORK_ROOT)
        self.tmp.cleanup()

    def test_mine_finds_commit_with_tests(self):
        d = call("mine", "--repo", str(self.repo))
        top = d["small"][0]
        self.assertEqual(top["subject"], "Add clamp")
        self.assertTrue(top["has_tests"])
        self.assertIn("tests/test_calc.py", top["test_files"])
        self.assertEqual(top["code_files"], ["calc.py"])

    def test_task_folder_has_no_future_history(self):
        b = barton.Barton(self.repo)
        folder = barton.make_folder(b, self.fix + "^", "f1")
        self.assertFalse((folder / "later.txt").exists())
        self.assertNotIn("clamp", (folder / "calc.py").read_text())
        log = sh(folder, "log", "--all", "--format=%s").split()
        self.assertEqual(log, ["start"])  # nothing from the user's history

    def test_barton_folder_is_git_excluded(self):
        b = barton.Barton(self.repo)
        b.init()
        (b.dir / "x.json").write_text("{}")
        self.assertEqual(sh(self.repo, "status", "--porcelain"), "")

    def test_validate_and_check(self):
        spec = Path(self.tmp.name) / "t1.json"
        spec.write_text(json.dumps({
            "id": "t1", "kind": "write", "base": self.fix + "^", "reference": self.fix,
            "prompt": "Add clamp(x, lo, hi) to calc.py.",
            "check": {"command": f'"{sys.executable}" -m unittest tests.test_calc',
                      "files": ["tests/test_calc.py"]}}))
        call("task-add", "--repo", str(self.repo), str(spec))
        d = call("validate", "--repo", str(self.repo), "t1")
        self.assertTrue(d["valid"], d)

    def test_setup_runs_once_per_commit(self):
        count = Path(self.tmp.name) / "setups.txt"
        setup = (f'"{sys.executable}" -c "import pathlib; pathlib.Path(\'built.txt\').write_text(\'x\');'
                 f' open(r\'{count}\', \'a\').write(\'.\')"')
        call("config", "--repo", str(self.repo), "--setup", setup)
        b = barton.Barton(self.repo)
        one = barton.make_folder(b, self.fix + "^", "f1")
        two = barton.make_folder(b, self.fix + "^", "f2", {"calc.py": b"x = 1\n"}, "change")
        self.assertEqual(count.read_text(), ".")  # built once, copied for the second folder
        for f in (one, two):
            self.assertTrue((f / "built.txt").exists())
            self.assertEqual(sh(f, "status", "--porcelain"), "")
        self.assertEqual(sh(two, "log", "--format=%s").split(), ["change", "setup", "start"])
        call("clean")
        self.assertFalse(barton.template_root().exists())

    def test_run_all_runs_tasks_at_once_and_cleans_up(self):
        for i in range(3):
            spec = Path(self.tmp.name) / f"t{i}.json"
            spec.write_text(json.dumps({
                "id": f"t{i}", "kind": "write", "base": self.fix + "^", "reference": self.fix,
                "prompt": "Add clamp(x, lo, hi) to calc.py.",
                "check": {"command": f'"{sys.executable}" -m unittest tests.test_calc',
                          "files": ["tests/__init__.py", "tests/test_calc.py"]}}))
            call("task-add", "--repo", str(self.repo), str(spec))
        solution = sh(self.repo, "show", f"{self.fix}:calc.py")

        def fake_model(is_error: bool) -> list[str]:
            # Solves the task, takes two seconds, and answers like `claude -p`.
            fake = Path(self.tmp.name) / f"fake{int(is_error)}.py"
            fake.write_text(
                "import json, pathlib, time\n"
                f"pathlib.Path('calc.py').write_text({solution!r})\n"
                "time.sleep(2)\n"
                "print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'done',"
                f" 'is_error': {is_error}, 'usage': {{}}, 'modelUsage': {{'m': {{}}}}}}))\n")
            return [sys.executable, str(fake)]

        def run(*extra: str, is_error=False) -> float:
            with unittest.mock.patch.object(barton, "harness_cmd", return_value=fake_model(is_error)), \
                    contextlib.redirect_stdout(io.StringIO()):
                t0 = time.time()
                barton.main(["run", "--repo", str(self.repo), "all", "--harness", "claude",
                             "--model", "m", *extra])
                return time.time() - t0

        took = run()
        outcomes = [r["outcome"] for r in barton.Barton(self.repo).results()]
        self.assertEqual(outcomes, ["solved"] * 3)
        self.assertLess(took, 6)  # one after another would take at least 3 x 2 s
        self.assertFalse(barton.WORK_ROOT.exists())  # no task folder is left behind
        run("--again", is_error=True)
        self.assertEqual([r["outcome"] for r in barton.Barton(self.repo).results()][3:],
                         ["error"] * 3)
        self.assertFalse(barton.WORK_ROOT.exists())  # also not after failed runs

    def test_plant_flips_a_comparison_and_grading_finds_it(self):
        d = call("plant", "--repo", str(self.repo), "r1", self.fix, "--seed", "1")
        self.assertEqual(d["file"], "calc.py")
        self.assertNotEqual(d["before"], d["after"])
        b = barton.Barton(self.repo)
        task = b.task("r1")
        folder = barton.make_folder(b, task["base"], "r1", barton.review_files(b, task), "change")
        line = (folder / "calc.py").read_text().split("\n")[d["line"] - 1]
        self.assertEqual(line.strip(), d["after"])
        self.assertEqual(sh(folder, "log", "--format=%s").split("\n")[0], "change")
        reply = "Two issues.\n\n```json\n" + json.dumps(
            [{"file": "calc.py", "line": d["line"] + 1, "problem": "wrong bound"},
             {"file": "calc.py", "line": 1, "problem": "style"}]) + "\n```\n"
        g = barton.grade_review(task, folder, Path(self.tmp.name), reply)
        self.assertEqual(g["outcome"], "found")
        self.assertEqual(g["other_findings"], 1)

    def test_review_folder_drops_the_old_path_of_a_renamed_file(self):
        sh(self.repo, "mv", "calc.py", "mathx.py")
        (self.repo / "mathx.py").write_text((self.repo / "mathx.py").read_text() + "\n\ndef pos(x):\n    if x > 0:\n        return x\n    return 0\n")
        sh(self.repo, "commit", "-q", "-am", "Rename calc and add pos")
        sha = sh(self.repo, "rev-parse", "HEAD").strip()
        d = call("plant", "--repo", str(self.repo), "r2", sha, "--seed", "1")
        b = barton.Barton(self.repo)
        task = b.task("r2")
        folder = barton.make_folder(b, task["base"], "r2", barton.review_files(b, task), "change")
        self.assertTrue((folder / "mathx.py").exists())
        self.assertFalse((folder / "calc.py").exists())
        self.assertEqual(d["file"], "mathx.py")

    def test_mine_counts_a_merged_branch_as_one_commit(self):
        sh(self.repo, "checkout", "-q", "-b", "feature")
        (self.repo / "calc.py").write_text((self.repo / "calc.py").read_text() + "\n\ndef neg(x):\n    return -x\n")
        sh(self.repo, "commit", "-q", "-am", "Add neg")
        sh(self.repo, "checkout", "-q", "main")
        sh(self.repo, "merge", "-q", "--no-ff", "-m", "Merge pull request #1", "feature")
        subjects = [c["subject"] for g in ("small", "medium", "large")
                    for c in call("mine", "--repo", str(self.repo))[g]]
        self.assertIn("Merge pull request #1", subjects)
        self.assertNotIn("Add neg", subjects)

    def test_review_without_file_goes_to_the_judge(self):
        task = {"plant": {"file": "a.py", "line": 3}}
        tmp = Path(self.tmp.name)
        self.assertEqual(barton.grade_review(task, tmp, tmp)["outcome"], "missed")
        g = barton.grade_review(task, tmp, tmp, "Line 3 of a.py flips the bound.")
        self.assertEqual((g["outcome"], g["method"]), ("pending", "judge"))

    def test_tasks_with_results_are_frozen(self):
        b = barton.Barton(self.repo)
        b.append_result({"run": "x", "task": "t9", "kind": "write", "model": "m",
                         "harness": "codex", "outcome": "solved", "attempt": 1})
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            barton.save_task(b, {"id": "t9", "kind": "write"})

    def test_anonymize_removes_folder_paths_and_names(self):
        runs = [{"run": "t1-gpt-6.2-sol-low-1", "model": "gpt-6.2-sol"}]
        text = ("See [a.py:4](/C:/Users/me/.barton/w/t1-3fa9c2/src/a.py:4) and "
                "/home/me/.barton/w/t1-3fa9c2/b.py; I am gpt-6.2-sol.")
        clean = barton.anonymize(text, runs)
        self.assertNotIn(".barton", clean)
        self.assertNotIn("gpt-6.2-sol", clean)
        self.assertIn("src/a.py:4", clean)

    def test_report_embeds_data(self):
        b = barton.Barton(self.repo)
        b.append_result({"run": "x", "task": "t1", "kind": "write", "model": "m",
                         "harness": "codex", "outcome": "pending", "attempt": 1})
        b.append_result({"run": "x", "task": "t1", "kind": "write", "model": "m",
                         "harness": "codex", "outcome": "solved", "attempt": 1})
        d = call("report", "--repo", str(self.repo), "--no-open")
        page = Path(d["report"]).read_text(encoding="utf-8")
        self.assertNotIn("/*BARTON_DATA*/null", page)
        self.assertEqual(len(barton.latest_rows(b.results())), 1)
        self.assertEqual(barton.latest_rows(b.results())[0]["outcome"], "solved")


class QualityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = Path(self.tmp.name)
        sh(repo, "init", "-q", "-b", "main")
        self.b = barton.Barton(repo)
        self.b.save_config({"tasks": [{"id": "t1", "kind": "write"}]})
        for run, model in (("x", "m1"), ("y", "m2")):
            self.b.append_result({"run": run, "task": "t1", "kind": "write", "model": model,
                                  "harness": "codex", "effort": "low", "outcome": "solved",
                                  "attempt": 1})

    def tearDown(self):
        self.tmp.cleanup()

    def prefer(self, first, second, choice):
        self.b.init()
        with (self.b.dir / "prefs.jsonl").open("a") as f:
            f.write(json.dumps({"task": "t1", "first": first, "second": second,
                                "choice": choice, "judge_model": "j"}) + "\n")

    def test_win_needs_both_orders_to_agree(self):
        self.prefer("x", "y", "first")
        self.assertEqual(barton.quality(self.b), [])  # one order only
        self.prefer("y", "x", "second")
        self.assertEqual(barton.quality(self.b)[0]["winner"], "m1|codex|low")

    def test_disagreeing_orders_are_a_tie(self):
        self.prefer("x", "y", "first")
        self.prefer("y", "x", "first")  # position bias: always the first
        self.assertIsNone(barton.quality(self.b)[0]["winner"])


class PromptTest(unittest.TestCase):
    def test_prompt_reaches_the_cli_whole_on_stdin(self):
        # Windows .cmd shims cut an argument at its first line break, so no prompt goes in argv.
        for h in ("claude", "codex"):
            with unittest.mock.patch.object(barton, "resolve_cli", return_value=["x"]):
                cmd = barton.harness_cmd(h, "m", "low", Path("."))
            self.assertFalse(any("\n" in a for a in cmd))
        echo = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
        text = "Task.\n\nRules after a blank line."
        got = barton.run_cmd(echo, Path("."), 60, stdin_text=text)[1]
        self.assertEqual(got.splitlines(), text.splitlines())

    def test_agy_gets_the_prompt_whole_as_one_argument(self):
        # agy reads no prompt from stdin; it is an .exe, so a multi-line argument survives.
        text = 'Task.\n\nRules with "quotes" after a blank line.'
        with unittest.mock.patch.object(barton, "resolve_cli", return_value=["agy.exe"]):
            cmd = barton.harness_cmd("agy", "gemini-4-argon-medium", None, Path("."), text)
        self.assertEqual(cmd[cmd.index("-p") + 1], text)
        self.assertEqual(cmd[cmd.index("--model") + 1], "gemini-4-argon-medium")
        self.assertFalse(barton.prompt_on_stdin("agy"))
        echo = [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", text]
        self.assertEqual(barton.run_cmd(echo, Path("."), 60)[1].splitlines(), text.splitlines())
        with unittest.mock.patch.object(barton, "resolve_cli", return_value=["agy.cmd"]), \
                self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            barton.harness_cmd("agy", "m", None, Path("."), text)


class ParseTest(unittest.TestCase):
    def test_size_buckets(self):
        self.assertEqual([barton.size_of(n) for n in (3, 24, 25, 150, 151)],
                         ["small", "small", "medium", "medium", "large"])

    def test_claude_json(self):
        out = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "total_cost_usd": 0.12, "usage": {"input_tokens": 10,
                          "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100,
                          "output_tokens": 7}, "modelUsage": {"claude-opus-5-5": {}}})
        u = barton.parse_usage("claude", out)
        self.assertEqual((u["input"], u["cached"], u["output"], u["cost_usd"]), (15, 100, 7, 0.12))
        self.assertIsNone(u["error"])

    def test_claude_login_error_is_an_error(self):
        out = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                          "result": "OAuth session expired", "usage": {}})
        self.assertIn("OAuth", barton.parse_usage("claude", out)["error"])

    def test_codex_events(self):
        lines = [{"type": "thread.started"},
                 {"type": "error", "message": "transient"},
                 {"type": "turn.completed", "usage": {"input_tokens": 100,
                  "cached_input_tokens": 60, "output_tokens": 9}}]
        u = barton.parse_usage("codex", "\n".join(json.dumps(x) for x in lines))
        self.assertEqual((u["input"], u["cached"], u["output"]), (40, 60, 9))
        self.assertIsNone(u["error"])
        self.assertEqual(barton.parse_usage("codex", "")["error"], "codex finished no turn")

    def test_agy_stream(self):
        step = lambda i, state, name: {"event": "step_update", "step_update": {
            "step_index": i, "state": state, "step_type": "tool", "tool_name": name}}
        lines = [{"event": "init", "init": {"model": "gemini-4-argon-medium"}},
                 step(1, "ACTIVE", "run_command"), step(1, "DONE", "run_command"),
                 step(2, "DONE", "search_web"), step(3, "DONE", "browser_get_dom"),
                 {"event": "result", "result": {"status": "SUCCESS", "response": "Done.",
                  "usage": {"input_tokens": 100, "output_tokens": 9, "thinking_tokens": 4,
                            "cache_read_tokens": 60}}}]
        u = barton.parse_usage("agy", "\n".join(json.dumps(x) for x in lines))
        self.assertEqual((u["input"], u["cached"], u["output"]), (100, 60, 13))
        self.assertEqual((u["model_seen"], u["web_used"], u["commands_ran"]),
                         ("gemini-4-argon-medium", 2, 3))
        self.assertEqual(u["final"], "Done.")
        self.assertIsNone(u["error"])
        self.assertEqual(barton.parse_usage("agy", "")["error"], "agy gave no result")
        failed = lines[:1] + [{"event": "result", "result": {"status": "ERROR", "error": "auth required"}}]
        self.assertIn("auth", barton.parse_usage("agy", "\n".join(json.dumps(x) for x in failed))["error"])

    def test_agy_refused_tools_are_recorded(self):
        lines = [{"event": "step_update", "step_update": {"state": "DENIED", "step_type": "tool",
                                                         "tool_name": "write_file"}},
                 {"event": "result", "result": {"status": "SUCCESS", "response": "", "usage": {}}}]
        u = barton.parse_usage("agy", "\n".join(json.dumps(x) for x in lines))
        self.assertEqual(u["tool_denials"], ["write_file: DENIED"])
        self.assertEqual(u["commands_ran"], 0)

    def test_estimated_cost(self):
        u = {"input": 1_000_000, "cached": 1_000_000, "output": 1_000_000}
        self.assertEqual(barton.estimate_cost("gpt-6.2-sol", u), 12.1)
        self.assertIsNone(barton.estimate_cost("unknown-model", u))


if __name__ == "__main__":
    unittest.main()
