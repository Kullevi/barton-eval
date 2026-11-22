"""Tests for barton.py: task folders, mining, bug tracing, grading and parsing.

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

# Coding tasks are switched off in this version (coming soon); their code is still tested.
barton.CODING_TASKS = True


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

    def test_stopping_barton_stops_what_its_runs_started(self):
        # Ctrl-C or a kill must not leave a model CLI or a check running on its own.
        for tid in ("t", "u"):
            spec = Path(self.tmp.name) / f"{tid}.json"
            spec.write_text(json.dumps({"id": tid, "kind": "write", "base": self.fix + "^",
                                        "reference": self.fix, "prompt": "Add clamp."}))
            call("task-add", "--repo", str(self.repo), str(spec))
        mark = Path(self.tmp.name) / "child-still-ran.txt"
        started = Path(self.tmp.name) / "started"
        started.mkdir()
        child = Path(self.tmp.name) / "child.py"
        child.write_text(f"import time, pathlib\ntime.sleep(4)\npathlib.Path({str(mark)!r}).write_text('x')\n")
        fake = Path(self.tmp.name) / "fake.py"
        fake.write_text(
            "import os, pathlib, subprocess, sys, time\n"
            f"pathlib.Path({str(started)!r}, os.path.basename(os.getcwd())).write_text('x')\n"
            "kw = {'start_new_session': True} if sys.platform != 'win32' else {}\n"
            f"subprocess.Popen([sys.executable, {str(child)!r}], **kw)\n"
            "time.sleep(60)\n")
        import signal
        import threading
        threading.Timer(2, signal.raise_signal, [signal.SIGINT]).start()
        try:
            with unittest.mock.patch.object(barton, "harness_cmd", return_value=[sys.executable, str(fake)]), \
                    unittest.mock.patch.object(barton.os, "_exit", side_effect=SystemExit(130)), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                barton.main(["run", "--repo", str(self.repo), "all", "--harness", "claude",
                             "--model", "m", "--jobs", "1"])
            self.assertEqual(barton.Barton(self.repo).results(), [])  # nothing cut short is recorded
            self.assertEqual(len(list(started.iterdir())), 1)  # the queued task never started
            self.assertEqual(list(barton.WORK_ROOT.glob("[tu]-*")), [])  # no task folder is left
            time.sleep(4)
            self.assertFalse(mark.exists())  # what the model started was stopped too
        finally:
            barton.STOPPING.clear()

    def test_a_stop_while_a_check_starts_in_the_main_thread_still_exits(self):
        # validate and plant start checks in the main thread, where the stop handler runs too.
        exits = []
        try:
            with unittest.mock.patch.object(barton.os, "_exit", side_effect=exits.append), \
                    unittest.mock.patch.object(barton.os, "write"):
                with barton._launch_lock:
                    barton.stop_everything()
            self.assertEqual(exits, [130])
        finally:
            barton.STOPPING.clear()

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

        # A model that runs out of time fails the task, and what it started is stopped too.
        mark = Path(self.tmp.name) / "child-still-ran.txt"
        child = Path(self.tmp.name) / "child.py"
        child.write_text(f"import time, pathlib\ntime.sleep(5)\npathlib.Path({str(mark)!r}).write_text('x')\n")
        slow = Path(self.tmp.name) / "slow.py"
        slow.write_text(f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, {str(child)!r}])\n"
                        "time.sleep(60)\n")
        with unittest.mock.patch.object(barton, "harness_cmd", return_value=[sys.executable, str(slow)]), \
                contextlib.redirect_stdout(io.StringIO()):
            t0 = time.time()
            barton.main(["run", "--repo", str(self.repo), "t0", "--harness", "claude", "--model", "m",
                         "--again", "--timeout", "2"])
        self.assertLess(time.time() - t0, 30)
        last = barton.Barton(self.repo).results()[-1]
        self.assertEqual((last["outcome"], last["method"]), ("failed", "timed out"), last)
        time.sleep(6)
        self.assertFalse(mark.exists())  # the model's own child process was stopped as well
        self.assertFalse(barton.WORK_ROOT.exists())

        # Codex whose sandbox cannot start runs no command at all: an error, not a failed task.
        boxed = Path(self.tmp.name) / "boxed.py"
        boxed.write_text(
            "import json\n"
            "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text':"
            " 'bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'}}))\n"
            "print(json.dumps({'type': 'turn.completed', 'usage': {}}))\n")
        with unittest.mock.patch.object(barton, "harness_cmd", return_value=[sys.executable, str(boxed)]), \
                contextlib.redirect_stdout(io.StringIO()):
            barton.main(["run", "--repo", str(self.repo), "t0", "--harness", "codex", "--model", "m",
                         "--again"])
        last = barton.Barton(self.repo).results()[-1]
        self.assertEqual(last["outcome"], "error", last)
        self.assertIn("codex-sandbox off", last["error"])

    @unittest.skipIf(os.name == "nt", "Windows ends them through the job object")
    def test_removing_a_task_folder_stops_what_still_runs_in_it(self):
        # Codex runs each command in a session of its own, outside the CLI's process group.
        folder = Path(self.tmp.name) / "w" / "task"
        folder.mkdir(parents=True)
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=folder,
                             start_new_session=True)
        time.sleep(0.5)
        barton.remove_tree(folder)
        self.assertIsNotNone(p.wait(timeout=10))
        self.assertFalse(folder.exists())

    def _bug_and_fix(self) -> str:
        """A change that introduces a bug among other work, and a later commit that fixes it."""
        (self.repo / "shop.py").write_text(
            "def price(n, rate=0):\n    net = n * 10\n    tax = net * rate\n    return net - tax\n"
            "\n\ndef label(n, rate=0):\n    return str(price(n, rate)) + ' kr'\n")
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-q", "-m", "Add shop with tax")
        intro = sh(self.repo, "rev-parse", "HEAD").strip()
        (self.repo / "notes.txt").write_text("unrelated work\n")
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-q", "-m", "Notes")
        text = (self.repo / "shop.py").read_text().replace("return net - tax", "return net + tax")
        (self.repo / "shop.py").write_text(text)
        sh(self.repo, "commit", "-q", "-am", "Fix tax being subtracted")
        return intro

    def test_bug_traces_a_fix_to_the_change_that_wrote_the_bug(self):
        intro = self._bug_and_fix()
        d = call("bug", "--repo", str(self.repo), "r1", "HEAD")
        self.assertEqual(d["introduced_by"], intro[:12])
        self.assertEqual(d["where"], [{"file": "shop.py", "line": 4, "end": 4}])
        b = barton.Barton(self.repo)
        task = b.task("r1")
        self.assertEqual((task["reference"], task["source"]), (intro, "fixed"))
        folder = barton.make_folder(b, task["base"], "r1", barton.review_files(b, task),
                                    barton.review_message(b, task))
        self.assertIn("return net - tax", (folder / "shop.py").read_text())  # the bug, as it was made
        self.assertFalse((folder / "notes.txt").exists())  # nothing later, the fix least of all
        self.assertEqual(sh(folder, "log", "-1", "--format=%s").strip(), "Add shop with tax")
        tmp = Path(self.tmp.name)
        reply = lambda fs: "Review.\n\n```json\n" + json.dumps(fs) + "\n```\n"
        # A finding at the place may be about something else: a judge decides, with the fix.
        g = barton.grade_review(task, folder, tmp, reply([{"file": "shop.py", "line": 4, "problem": "x"}]))
        self.assertEqual((g["outcome"], g["method"], g["at_the_place"]), ("pending", "judge", True))
        self.assertEqual(barton.grade_review(task, folder, tmp, reply([]))["outcome"], "missed")
        b.append_result({"run": "r1-m", "task": "r1", "kind": "review", "model": "m", "harness": "h",
                         "attempt": 1, "outcome": "pending"})
        (b.dir / "runs" / "r1-m").mkdir(parents=True)
        (b.dir / "runs" / "r1-m" / "final.txt").write_text("the tax is subtracted")
        key = Path(call("blind", "--repo", str(self.repo), "r1")["bundle"]).read_text()
        self.assertIn("Fix tax being subtracted", key)
        self.assertIn("+    return net + tax", key)

    def test_bug_refuses_a_commit_that_fixes_no_earlier_code(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            barton.main(["bug", "--repo", str(self.repo), "r1", self.fix])  # only adds new code

    def test_a_bug_a_review_found_becomes_one_more_answer_key(self):
        self._bug_and_fix()
        call("bug", "--repo", str(self.repo), "r1", "HEAD")
        b = barton.Barton(self.repo)
        for model, findings in (("m1", [{"file": "shop.py", "line": 8, "problem": "label has no space check"}]),
                                ("m2", [])):
            b.append_result({"run": f"r1-{model}-default-1", "task": "r1", "kind": "review", "model": model,
                             "harness": "h", "effort": None, "attempt": 1, "seconds": 30, "cost_usd": 0.5,
                             "outcome": "pending" if findings else "missed", "findings": len(findings)})
            d = b.dir / "runs" / f"r1-{model}-default-1"
            d.mkdir(parents=True)
            (d / "final.txt").write_text(f"review by {model}: {findings}")
            (d / "review.json").write_text(json.dumps(findings))
        got = call("findings", "--repo", str(self.repo), "r1")
        text = Path(got["bundle"]).read_text()
        self.assertIn("label has no space check", text)
        self.assertIn("Fix tax being subtracted", text)  # what is already counted
        self.assertNotIn("m1", text)
        # A real but small finding is recorded on its review and never on a found bug's row.
        self.assertEqual(call("small", "--repo", str(self.repo), "r1", "1")["by_run"], {"r1-m1-default-1": 1})
        self.assertEqual({r["model"]: r["small"] for r in barton.current_rows(b)}, {"m1": 1, "m2": 0})
        d = call("bug-found", "--repo", str(self.repo), "r1", "r1b", "--where", "shop.py:8",
                 "--what", "label() joins the price and the unit wrongly")
        self.assertEqual(d["reviews_to_judge"], 1)
        rows = {r["model"]: r for r in barton.current_rows(b) if r["task"] == "r1b"}
        # The same review answers for the new bug: no findings is a miss, the rest wait for
        # the judge, and the time and cost are not counted twice.
        self.assertEqual((rows["m1"]["outcome"], rows["m2"]["outcome"]), ("pending", "missed"))
        self.assertEqual((rows["m1"]["seconds"], rows["m1"]["cost_usd"], rows["m1"]["review_of"]),
                         (0, 0, "r1-m1-default-1"))
        self.assertNotIn("small", rows["m1"])
        key = Path(call("blind", "--repo", str(self.repo), "r1b")["bundle"]).read_text()
        self.assertIn("joins the price and the unit wrongly", key)
        self.assertIn("review by <model>", key)
        call("verdict", "--repo", str(self.repo), "r1b", "A", "found", "--judge-model", "j", "--note", "n")
        self.assertEqual({r["model"]: r["outcome"] for r in barton.current_rows(b) if r["task"] == "r1b"},
                         {"m1": "found", "m2": "missed"})
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            barton.main(["run", "--repo", str(self.repo), "r1b", "--harness", "claude", "--model", "m3"])
        self.assertEqual(call("task-remove", "--repo", str(self.repo), "r1")["tasks"], 0)

    def test_a_task_from_before_real_bugs_still_runs(self):
        b = barton.Barton(self.repo)
        task = {"id": "old", "kind": "review", "base": self.fix + "^", "reference": self.fix,
                "plant": {"file": "calc.py", "line": 6, "before": "if x < lo:", "after": "if x <= lo:"}}
        files = barton.review_files(b, task)
        self.assertIn(b"    if x <= lo:", files["calc.py"])
        self.assertIn("tests/test_calc.py", files)
        tmp = Path(self.tmp.name)
        reply = "```json\n" + json.dumps([{"file": "calc.py", "line": 7, "problem": "bound"}]) + "\n```"
        self.assertEqual(barton.grade_review(task, tmp, tmp, reply)["outcome"], "found")

    def test_clean_waits_for_a_command_that_uses_task_folders(self):
        folder = barton.WORK_ROOT / "x"
        folder.mkdir(parents=True)
        hold = subprocess.Popen([sys.executable, "-c",
            f"import sys, time; sys.path.insert(0, {str(Path(barton.__file__).parent)!r}); import barton\n"
            f"from pathlib import Path\nbarton.WORK_ROOT = Path({str(barton.WORK_ROOT)!r})\n"
            "with barton.working():\n    print('held', flush=True); time.sleep(30)\n"], stdout=subprocess.PIPE)
        try:
            hold.stdout.readline()
            with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
                barton.main(["clean"])
            self.assertTrue(folder.exists())
        finally:
            hold.kill()
            hold.wait()
            hold.stdout.close()
        call("clean")  # the lock went with its holder
        self.assertFalse(folder.exists())

    def test_coding_tasks_are_switched_off(self):
        spec = Path(self.tmp.name) / "t.json"
        spec.write_text(json.dumps({"id": "t1", "kind": "write", "base": self.fix + "^", "prompt": "x"}))
        buf = io.StringIO()
        with unittest.mock.patch.object(barton, "CODING_TASKS", False), self.assertRaises(SystemExit), contextlib.redirect_stdout(buf):
            barton.main(["task-add", "--repo", str(self.repo), str(spec)])
        self.assertIn("coming soon", buf.getvalue())

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

    def test_a_reply_without_a_bug_list_is_not_a_miss(self):
        # A model that refused, asked a question or stopped early did not do the review:
        # the run is an error to look at and do again, never a score.
        task = {"plant": {"file": "a.py", "line": 3}}
        tmp = Path(self.tmp.name)
        self.assertEqual(barton.grade_review(task, tmp, tmp)["outcome"], "error")
        g = barton.grade_review(task, tmp, tmp, "Before I do any work, I have to flag this.")
        self.assertEqual(g["outcome"], "error")
        self.assertIn("final.txt", g["error"])

    def _results(self, rows):
        b = barton.Barton(self.repo)
        b.save_config({"tasks": [{"id": t, "kind": "write", "check": {"command": "x"}}
                                 for t in ("a", "b", "c", "d")]})
        for task, model, outcome, secs in rows:
            b.append_result({"run": f"{task}-{model}", "task": task, "kind": "write", "model": model,
                             "harness": "claude", "outcome": outcome, "attempt": 1, "seconds": secs,
                             "at": "2026-11-01T10:00:00+00:00"})
        return b

    def test_quick_set_prefers_tasks_that_told_models_apart_then_fast_ones(self):
        self._results([("a", "m1", "solved", 900), ("a", "m2", "failed", 900),
                       ("b", "m1", "solved", 60), ("b", "m2", "solved", 60),
                       ("c", "m1", "solved", 120), ("c", "m2", "failed", 120),
                       ("d", "m1", "solved", 30), ("d", "m2", "solved", 30)])
        self.assertEqual(call("quick", "--repo", str(self.repo), "--coding", "3", "--reviews", "0")["quick"],
                         ["c", "a", "d"])

    def test_report_compares_models_on_the_same_tasks_only(self):
        self._results([("a", "m1", "solved", 1), ("a", "m2", "failed", 1), ("b", "m1", "solved", 1)])
        d = call("report", "--repo", str(self.repo), "--no-open")
        self.assertEqual((d["tasks"], d["left_out"]), (1, ["b"]))  # m2 has not run b yet

    def test_a_setting_with_few_results_is_left_off_rather_than_shrinking_the_rest(self):
        self._results([(t, m, "solved", 1) for t in "abcd" for m in ("m1", "m2")]
                      + [("a", "m3", "failed", 1)])  # m3 was stopped after one task
        d = call("report", "--repo", str(self.repo), "--no-open")
        self.assertEqual((d["tasks"], [p["setting"] for p in d["partial"]]),
                         (4, ["m3|claude|default"]))
        page = Path(d["report"]).read_text(encoding="utf-8")
        self.assertNotIn('"model": "m3"', page)

    def test_an_excluded_task_is_left_off_the_cards_with_its_reason(self):
        self._results([("a", "m1", "solved", 1), ("a", "m2", "failed", 1),
                       ("b", "m1", "failed", 1), ("b", "m2", "failed", 1)])
        why = "the check wants an issue code the prompt never names"
        call("exclude", "--repo", str(self.repo), "b", "--reason", why)
        d = call("report", "--repo", str(self.repo), "--no-open")
        self.assertEqual((d["tasks"], d.get("left_out")), (1, None))
        page = Path(d["report"]).read_text(encoding="utf-8")
        self.assertIn(why, page)
        self.assertNotIn("b", call("quick", "--repo", str(self.repo), "--reviews", "0")["quick"])
        cfg = barton.Barton(self.repo).config()
        cfg["quick"] = ["a", "b"]
        barton.Barton(self.repo).save_config(cfg)
        call("exclude", "--repo", str(self.repo), "b", "--reason", why)
        self.assertEqual(barton.Barton(self.repo).config()["quick"], ["a"])  # nor run with the quick set
        call("exclude", "--repo", str(self.repo), "b", "--undo")
        self.assertEqual(call("report", "--repo", str(self.repo), "--no-open")["tasks"], 2)

    def test_status_flags_a_judge_that_mostly_picks_one_position(self):
        b = self._results([])
        b.init()
        with (b.dir / "prefs.jsonl").open("a") as f:
            for i in range(12):
                f.write(json.dumps({"task": "a", "first": "x", "second": "y", "pair": str(i),
                                    "choice": "second" if i else "first", "judge_model": "j"}) + "\n")
        j = call("status", "--repo", str(self.repo))["judges"]["j"]
        self.assertEqual((j["first"], j["second"]), (1, 11))
        self.assertIn("stronger judge", j["note"])

    def test_run_budget_starts_no_task_after_it(self):
        self._results([])
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            with unittest.mock.patch.object(barton, "run_one", return_value={"outcome": "solved"}) as one, \
                    unittest.mock.patch.object(barton.time, "time", side_effect=[0, 0] + [10_000] * 20):
                barton.main(["run", "--repo", str(self.repo), "all", "--harness", "claude",
                             "--model", "m", "--budget", "30", "--jobs", "1"])
        self.assertEqual(one.call_count, 1)  # the first started in time; the rest did not start
        self.assertIn("budget was used up", buf.getvalue())

    def test_check_timeout_is_set_per_project(self):
        b = barton.Barton(self.repo)
        self.assertEqual(barton.check_timeout(b), barton.CHECK_TIMEOUT)
        call("config", "--repo", str(self.repo), "--check-timeout", "120")
        self.assertEqual(barton.check_timeout(b), 7200)

    def test_unknown_cost_stays_unknown_in_status(self):
        b = barton.Barton(self.repo)
        b.save_config({"tasks": [{"id": "t0.5", "kind": "write"}, {"id": "tNone", "kind": "write"}]})
        for cost in (0.5, None):
            b.append_result({"run": f"r{cost}", "task": f"t{cost}", "kind": "write", "model": "g",
                             "harness": "agy", "outcome": "solved", "attempt": 1, "seconds": 1,
                             "cost_usd": cost})
        self.assertIsNone(call("status", "--repo", str(self.repo))["by_model"]["g (agy, default)"]["cost_usd"])

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

    def prefer(self, first, second, choice, judge="j"):
        self.b.init()
        with (self.b.dir / "prefs.jsonl").open("a") as f:
            f.write(json.dumps({"task": "t1", "first": first, "second": second,
                                "choice": choice, "judge_model": judge}) + "\n")

    def test_forgetting_a_weak_judge_lets_the_other_decide(self):
        self.prefer("x", "y", "first", "weak"); self.prefer("y", "x", "first", "weak")
        self.prefer("x", "y", "first", "strong"); self.prefer("y", "x", "second", "strong")
        self.assertIsNone(barton.quality(self.b)[0]["winner"])
        with contextlib.redirect_stdout(io.StringIO()):
            barton.main(["forget-judge", "--repo", str(self.b.repo), "weak"])
        self.assertEqual(barton.quality(self.b)[0]["winner"], "m1|codex|low")

    def test_two_judges_may_take_one_order_each(self):
        self.prefer("x", "y", "first", "j1"); self.prefer("y", "x", "second", "j2")
        self.assertEqual(barton.quality(self.b)[0]["winner"], "m1|codex|low")

    def test_judges_that_disagree_make_a_tie_and_agreeing_judges_a_win(self):
        self.prefer("x", "y", "first", "j1"); self.prefer("y", "x", "second", "j1")
        self.prefer("x", "y", "second", "j2"); self.prefer("y", "x", "first", "j2")
        self.assertIsNone(barton.quality(self.b)[0]["winner"])  # one judge must not overwrite the other
        (self.b.dir / "prefs.jsonl").unlink()
        self.prefer("x", "y", "first", "j1"); self.prefer("y", "x", "second", "j1")
        self.prefer("x", "y", "first", "j2"); self.prefer("y", "x", "second", "j2")
        self.assertEqual(barton.quality(self.b)[0]["winner"], "m1|codex|low")

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

    def test_a_review_may_only_read(self):
        with unittest.mock.patch.object(barton, "resolve_cli", return_value=["x.exe"]):
            claude = barton.harness_cmd("claude", "m", None, Path("."), review=True)
            codex = barton.harness_cmd("codex", "m", None, Path("."), review=True)
            agy = barton.harness_cmd("agy", "m", None, Path("."), "p", review=True)
        self.assertEqual(claude[claude.index("--tools") + 1], "Read,Grep,Glob")
        self.assertNotIn("--allowedTools", claude)
        self.assertIn("read-only", codex)
        self.assertNotIn("--dangerously-skip-permissions", agy)
        settings = json.loads(agy[agy.index("--settings") + 1])
        self.assertIn("command(*)", settings["permissions"]["deny"])

    def test_a_review_that_read_the_project_itself_is_found(self):
        home = Path.home().resolve()
        b = unittest.mock.Mock(repo=home / "code" / "app")
        folder = home / ".barton" / "w" / "t1"
        went = barton.outside_reads(b, folder, {"commands": [
            "rg -n foo src/main.py",
            "powershell -Command \"Get-Content (Join-Path $HOME 'code\\app\\src\\main.py')\"",
            f"cat {(home / 'code' / 'app' / 'x.py').as_posix()}",
            "cat ~/code/application/x.py | head",
        ]})
        self.assertEqual(len(went), 2)  # not the neighbour whose name starts alike
        self.assertEqual(barton.outside_reads(b, folder, {"commands": ["git show HEAD", "rg app"]}), [])
        # agy names each file it opens.
        paths = {"paths": [str(folder / "src" / "a.py"), str(home / "notes.txt")]}
        self.assertEqual(barton.outside_reads(b, folder, paths), [str(home / "notes.txt")])

    def test_codex_sandbox_can_be_turned_off_and_reads_instructions_whole(self):
        with unittest.mock.patch.object(barton, "resolve_cli", return_value=["x"]):
            on = barton.harness_cmd("codex", "m", "low", Path("."))
            off = barton.harness_cmd("codex", "m", "low", Path("."), sandbox=False)
        self.assertIn("workspace-write", on)
        self.assertIn("danger-full-access", off)
        self.assertNotIn("workspace-write", off)
        self.assertIn("project_doc_max_bytes=1048576", on)

    def test_check_tells_which_clis_were_used_here(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / ".codex").mkdir()
            with unittest.mock.patch.object(Path, "home", return_value=Path(home)), \
                    unittest.mock.patch.object(barton, "which", return_value=None):
                d = call("check")
        self.assertEqual({k: d[k]["used_here"] for k in ("claude", "codex", "agy")},
                         {"claude": False, "codex": True, "agy": False})

    @unittest.skipUnless(os.name == "nt", "Windows error boxes")
    def test_a_crash_two_programs_down_ends_without_an_error_box(self):
        # PowerShell resets the inherited error mode, so a crash in a program it starts
        # would open a box and hang until clicked. Barton's job object prevents that.
        here = Path(__file__).resolve().parents[1]
        crash = "import faulthandler; faulthandler._read_null()"
        script = (f"import subprocess, sys; sys.path.insert(0, {str(here)!r}); import barton; "
                  "assert barton.contain_crashes(); "
                  "ps = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', "
                  f"\"& '\" + sys.executable + \"' -c '{crash}'; Write-Output $LASTEXITCODE\"]; "
                  "print(subprocess.run(ps, capture_output=True, text=True, "
                  "creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip())")
        p = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True,
                           timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(p.stdout.strip(), "-1073741819", p.stderr)  # access violation, no box

    def test_agy_gets_the_prompt_whole_as_one_argument(self):
        # agy reads no prompt from stdin; it is an .exe, so a multi-line argument survives.
        text = 'Task.\n\nRules with "quotes" after a blank line.'
        with unittest.mock.patch.object(barton, "resolve_cli", return_value=["agy.exe"]):
            cmd = barton.harness_cmd("agy", "gemini-4-argon-medium", None, Path("."), text)
        self.assertEqual(cmd[cmd.index("-p") + 1], text)
        self.assertEqual(cmd[cmd.index("--model") + 1], "gemini-4-argon-medium")
        with unittest.mock.patch.object(barton, "resolve_cli", return_value=["agy.exe"]):
            cmd = barton.harness_cmd("agy", "gemini-4-argon", "high", Path("."), text)
        self.assertEqual(cmd[cmd.index("--model") + 1], "gemini-4-argon-high")
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
        self.assertEqual(barton.estimate_cost("gemini-4-argon", u, "2026-11-20"), 12.1)
        self.assertEqual(barton.estimate_cost("gemini-4-argon", u, "2026-12-01"), 24.2)
        self.assertIsNone(barton.estimate_cost("gemini-4-argon", u, "2026-01-01"))


if __name__ == "__main__":
    unittest.main()
