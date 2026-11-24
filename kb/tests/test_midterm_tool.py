"""The midterm evaluation may be run once, on the record: these tests guard the rules around the run (not its numbers).
A midterm that can be repeated until it looks good, overwritten, or started without a trace is not a midterm (S3 plan C8)."""
import importlib.util
import json
import os
import sys
import unittest
from unittest import mock

from kb import commit

from .support import tmpdir
from .world import World

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "s3_midterm_eval.py")


def text_of(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def load():
    spec = importlib.util.spec_from_file_location("s3_midterm_eval_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir(self)
        self.m = load()
        self.ledger_path = os.path.join(self.d, "ledger.jsonl")
        gap = os.path.join(self.d, "gap.json")
        with open(gap, "w", encoding="utf-8") as f:
            json.dump({"tasks": []}, f)
        self.s1 = os.path.join(self.d, "s1") + "/"                                      # a stand-in for the frozen folder: the real one is not part of the code
        os.makedirs(self.s1 + "a0")
        os.makedirs(self.s1 + "tasks")
        with open(self.s1 + "tasks/pdf_tasks.json", "w", encoding="utf-8") as f:
            f.write("[]")
        for name in ("real", "blind1", "blind2"):
            with open(self.s1 + f"a0/{name}.json", "w", encoding="utf-8") as f:
                json.dump({"results": [{"id": name + "-1", "query": f"the real {name} query"}]}, f)
        for name, value in (("OUT_DIR", self.d), ("LEDGER", self.ledger_path), ("GAP_FILE", gap), ("S1", self.s1)):
            patcher = mock.patch.object(self.m, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, *argv, evaluate=None):
        fake = evaluate or mock.Mock(side_effect=lambda parent, synthetic, n: {"run_number": n, "marker": f"result {n}", "pdf_gap_group": {"kb": {}}})
        with mock.patch.object(sys, "argv", ["s3_midterm_eval.py", "parent", *argv]), mock.patch.object(self.m, "evaluate", fake), \
                mock.patch("builtins.print"):
            self.m.main()
        return fake

    def entries(self):
        if not os.path.isfile(self.ledger_path):
            return []
        with open(self.ledger_path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def result(self, n):
        with open(os.path.join(self.d, f"s3_midterm_run{n}.json"), encoding="utf-8") as f:
            return json.load(f)


class TestOneAttemptOnTheRecord(Base):
    def test_the_attempt_is_on_the_record_before_the_first_task_is_read_and_ends_completed(self):
        seen = {}

        def evaluate(parent, synthetic, n):
            seen["ledger"] = [r["kind"] for r in self.entries()]
            return {"run_number": n, "marker": "first", "pdf_gap_group": {"kb": {}}}
        self.run_main(evaluate=mock.Mock(side_effect=evaluate))
        self.assertEqual(seen["ledger"], ["attempt", "provenance"])                     # already written when the evaluation starts
        self.assertEqual([r["kind"] for r in self.entries()], ["attempt", "provenance", "completed"])
        self.assertEqual(self.result(1)["marker"], "first")

    def test_a_second_attempt_is_refused_without_a_stated_reason_and_changes_nothing(self):
        self.run_main()
        before = text_of(self.ledger_path)
        fake = mock.Mock()
        with self.assertRaises(SystemExit) as cm:
            self.run_main(evaluate=fake)
        self.assertIn("refused", str(cm.exception))
        fake.assert_not_called()
        self.assertEqual(text_of(self.ledger_path), before)
        self.assertFalse(os.path.exists(os.path.join(self.d, "s3_midterm_run2.json")))

    def test_a_stated_reason_allows_another_attempt_and_the_first_result_stays(self):
        self.run_main()
        self.run_main("--again", "the model server crashed at query 12")
        self.assertEqual(self.result(1)["marker"], "result 1")
        self.assertEqual(self.result(2)["marker"], "result 2")
        attempts = [r for r in self.entries() if r["kind"] == "attempt"]
        self.assertEqual([(r["n"], r["reason"]) for r in attempts], [(1, None), (2, "the model server crashed at query 12")])

    def test_an_attempt_that_failed_still_counts_and_the_error_is_kept(self):
        with self.assertRaises(self.m.Abort):
            self.run_main(evaluate=mock.Mock(side_effect=self.m.Abort("the frozen files do not verify")))
        kinds = [r["kind"] for r in self.entries()]
        self.assertEqual(kinds, ["attempt", "provenance", "aborted"])
        self.assertIn("do not verify", self.entries()[-1]["error"])
        self.assertFalse(os.path.exists(os.path.join(self.d, "s3_midterm_run1.json")))
        with self.assertRaises(SystemExit):                                           # a failure is not a free retry
            self.run_main()

    def test_an_earlier_result_file_is_never_overwritten(self):
        path = os.path.join(self.d, "s3_midterm_run1.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("an earlier result")
        with self.assertRaises(FileExistsError):
            self.run_main()
        self.assertEqual(text_of(path), "an earlier result")
        self.assertEqual([r["kind"] for r in self.entries()], ["attempt", "provenance", "aborted"])

    def test_a_process_that_started_together_with_another_stops_if_it_was_not_first(self):
        """Two starts that both found the ledger empty: the earlier line proceeds, the later one stops before reading a task."""
        real_append = self.m.ledger_append
        calls = []

        def racing(rec):
            if not calls:
                calls.append(1)
                real_append({"kind": "attempt", "n": 1, "at": "t", "reason": None, "token": "the other process"})
            real_append(rec)
        fake = mock.Mock()
        with mock.patch.object(self.m, "ledger_append", racing):
            with self.assertRaises(self.m.Abort) as cm:
                self.run_main(evaluate=fake)
        self.assertIn("another process", str(cm.exception))
        fake.assert_not_called()
        self.assertEqual([r["kind"] for r in self.entries()], ["attempt", "attempt", "aborted"])

    def test_the_completed_record_binds_the_result_file_by_its_digest(self):
        import hashlib
        self.run_main()
        done = self.entries()[-1]
        self.assertEqual(done["kind"], "completed")
        with open(os.path.join(self.d, done["result_file"]), "rb") as f:
            self.assertEqual(done["result_sha256"], hashlib.sha256(f.read()).hexdigest())

    def test_the_provenance_of_the_inputs_is_recorded_after_the_attempt(self):
        self.run_main()
        kinds = [r["kind"] for r in self.entries()]
        self.assertEqual(kinds, ["attempt", "provenance", "completed"])
        self.assertEqual(set(self.entries()[1]) - {"kind", "n"}, {"script_sha256", "gap_file_sha256", "frozen_task_file_sha256"})

    def test_a_dry_run_leaves_no_trace_and_no_result(self):
        fake = self.run_main("--synthetic", os.path.join(self.d, "made_up"))
        fake.assert_called_once()
        self.assertEqual(fake.call_args[0][1], os.path.join(self.d, "made_up").replace(chr(92), "/"))
        self.assertEqual(self.entries(), [])
        self.assertEqual([n for n in os.listdir(self.d) if n.startswith("s3_midterm_run")], [])
        self.run_main()                                                                # and it did not use up the one attempt
        self.assertEqual(self.result(1)["marker"], "result 1")


class TestTheEvaluationItself(Base):
    def test_frozen_files_that_do_not_verify_stop_the_run_before_the_data_is_touched(self):
        bad = mock.Mock(returncode=1, stdout="changed: a0/real.json", stderr="")
        with mock.patch("subprocess.run", return_value=bad), mock.patch.object(self.m, "Home", side_effect=AssertionError("the home was opened")):
            with self.assertRaises(self.m.Abort) as cm:
                self.m.evaluate(self.d, None, 1)
        self.assertIn("do not verify", str(cm.exception))

    def test_a_dry_run_that_uses_a_real_task_query_is_refused_before_the_home_is_opened(self):
        """Made-up tasks only: a dry run is not a way to look at the real tasks' results without the record."""
        syn = os.path.join(self.d, "syn")
        os.makedirs(syn + "/a0")
        for name in ("real", "blind1", "blind2"):
            with open(syn + f"/a0/{name}.json", "w", encoding="utf-8") as f:
                json.dump({"results": [{"id": "x", "query": f"made up for {name}"}]}, f)
        with open(syn + "/gap.json", "w", encoding="utf-8") as f:
            json.dump({"tasks": [{"id": "y", "query": "the real blind1 query"}]}, f)             # one real query slipped in
        with mock.patch.object(self.m, "Home", side_effect=AssertionError("the home was opened")):
            with self.assertRaises(self.m.Abort) as cm:
                self.m.evaluate(self.d, syn.replace(chr(92), "/"), 0)
        self.assertIn("made-up tasks", str(cm.exception))

    def gap_file_with(self, **changes):
        tasks = [{"id": "a", "group": "gap", "query": "q1"}, {"id": "b", "group": "gap", "query": "q2"}]
        meta = {"derived_from_sha256": self.m.sha256_bytes_of(self.s1 + "tasks/pdf_tasks.json"), "group": "gap", "tasks": tasks,
                "tasks_sha256": self.m.tasks_digest(tasks)}
        meta.update(changes)
        path = os.path.join(self.d, "cut.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        return path

    def test_a_gap_file_that_is_what_the_cutter_wrote_passes(self):
        with mock.patch.object(self.m, "GAP_N", 2):
            self.assertEqual(len(self.m.check_gap_meta(self.gap_file_with())["tasks"]), 2)

    def test_a_gap_file_is_refused_when_it_is_not_a_cut_of_the_frozen_file_or_was_edited(self):
        tampered = [{"id": "a", "group": "gap", "query": "changed"}, {"id": "b", "group": "gap", "query": "q2"}]
        cases = {"another source": {"derived_from_sha256": "0" * 64},
                 "the wrong group": {"group": "acceptance"},
                 "an edited task": {"tasks": tampered},                                  # the recorded digest no longer fits
                 "a dropped task": {"tasks": tampered[1:], "tasks_sha256": self.m.tasks_digest(tampered[1:])}}      # digest fits, the count does not
        with mock.patch.object(self.m, "GAP_N", 2):
            for label, change in cases.items():
                with self.subTest(label), self.assertRaises(self.m.Abort):
                    self.m.check_gap_meta(self.gap_file_with(**change))

    def test_a_task_that_is_not_in_the_gap_group_is_refused_wherever_the_file_comes_from(self):
        path = os.path.join(self.d, "mixed.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"tasks": [{"id": "a", "group": "gap", "query": "q"}, {"id": "z", "group": "acceptance", "query": "r"}]}, f)
        with self.assertRaises(self.m.Abort):
            self.m.load_gap_tasks(path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"tasks": []}, f)
        with self.assertRaises(self.m.Abort):
            self.m.load_gap_tasks(path)

    def test_the_digest_of_the_gap_tasks_changes_with_any_change_to_a_task(self):
        tasks = [{"id": "a", "query": "q", "gold": {"pdf": "x.pdf", "page": 1}}, {"id": "b", "query": "r", "gold": {"pdf": "y.pdf", "page": 2}}]
        base = self.m.tasks_digest(tasks)
        self.assertEqual(base, self.m.tasks_digest([dict(t) for t in tasks]))
        self.assertNotEqual(base, self.m.tasks_digest(tasks[:1]))
        changed = [dict(tasks[0], query="other"), tasks[1]]
        self.assertNotEqual(base, self.m.tasks_digest(changed))

    def test_the_frozen_task_file_that_holds_the_acceptance_group_is_only_hashed_never_opened(self):
        with open(TOOL, encoding="utf-8") as f:
            lines = [ln for ln in f if "pdf_tasks.json" in ln and not ln.lstrip().startswith(("#", '"""'))]
        self.assertTrue(lines)
        for ln in lines:
            self.assertIn("sha256_bytes_of(", ln)                                      # the one use: proving the gap file is cut from it

    def test_the_arguments_take_a_reason_and_a_dry_run_folder(self):
        parent, opts = self.m.parse(["p", "--again", "why", "--synthetic", "C:/x/y/"])
        self.assertEqual((parent, opts), ("p", {"again": "why", "synthetic": "C:/x/y"}))
        with self.assertRaises(SystemExit):
            self.m.parse(["a", "b"])


class TestActiveFilesCheck(World):
    def test_a_commit_whose_file_was_damaged_without_changing_its_size_stops_the_run(self):
        """Serving would accept it (sizes only); a once-only run must not score on it."""
        m = load()
        self.seed_docs()
        r = self.build()
        m.check_active_files(self.home, r.commit_id)                                  # healthy: nothing raised
        path = os.path.join(commit.segment_dir(self.home, r.commit_id), commit.FTS)
        with open(path, "r+b") as f:
            f.seek(-5, os.SEEK_END)
            last = f.read(1)
            f.seek(-5, os.SEEK_END)
            f.write(bytes([last[0] ^ 0xFF]))
        commit.active_artifacts(self.home, self.conn)                                 # the quick check does not notice
        with self.assertRaises(m.Abort):
            m.check_active_files(self.home, r.commit_id)


if __name__ == "__main__":
    unittest.main()
