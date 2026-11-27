"""bench/labeling/labeling.py: the labelled-corpus pipeline, on tiny synthetic data (no network, no `gh`).

Each test says why the behaviour matters. The pipeline's output is the reference answer another model is scored
against, so a silent mistake here (an annotator who saw the repo name, a disputed row counted as agreed, a wrong
kappa) corrupts every number measured afterwards."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bench", "labeling"))
import labeling as lb  # noqa: E402

BODY = "x" * 90


def issue(number, title="t", body=BODY, created="2026-10-01T00:00:00Z", user="User", pr=False, labels=()):
    d = {"number": number, "title": title, "body": body, "created_at": created, "user": {"type": user},
         "html_url": f"https://github.com/o/r/issues/{number}", "labels": [{"name": n} for n in labels]}
    if pr:
        d["pull_request"] = {}
    return d


def fake_gh(pages):
    """pages: the issues a repo holds, served as two back-to-back JSON arrays like `gh api --paginate`."""
    def gh(args):
        half = len(pages) // 2
        return json.dumps(pages[:half]) + "\n" + json.dumps(pages[half:])
    return gh


def refused(testcase, fn, why):
    """The call must be refused with a clean SystemExit message. Any other exception is a crash that happens to look
    like a refusal, and a quiet success is the failure this is here to catch."""
    try:
        fn()
    except SystemExit:
        return
    except Exception as e:                      # noqa: BLE001
        testcase.fail(f"{why}: expected a clean refusal, got {type(e).__name__}: {e}")
    testcase.fail(f"{why}: accepted")


def tmp(testcase):
    d = tempfile.mkdtemp(prefix="lbl_test_")
    import shutil
    testcase.addCleanup(shutil.rmtree, d, True)
    return d


class TestFetch(unittest.TestCase):
    def test_only_real_human_issues_with_a_body_after_the_start_date_are_drawn(self):
        """A pull request, a bot post, a two-word issue or an old one in the pool would be labelled by the
        annotators and then counted as a message the agent will see; none of them is."""
        pages = [issue(1), issue(2, pr=True), issue(3, user="Bot"), issue(4, body="short"),
                 issue(5, created="2026-01-01T00:00:00Z"), issue(6, labels=("bug", "a"))]
        rows = lb.fetch_issues([("o/r", 10)], "2026-09-15T00:00:00Z", 700, 1, set(), gh=fake_gh(pages))
        self.assertEqual(sorted(r["id"] for r in rows), ["r-1", "r-6"])
        self.assertEqual([r for r in rows if r["id"] == "r-6"][0]["gh_labels"], ["a", "bug"])

    def test_ids_from_an_earlier_trial_are_left_out(self):
        pages = [issue(1), issue(2)]
        rows = lb.fetch_issues([("o/r", 10)], "2026-09-15T00:00:00Z", 700, 1, {"r-1"}, gh=fake_gh(pages))
        self.assertEqual([r["id"] for r in rows], ["r-2"])

    def test_text_is_title_blank_line_body_cut_to_the_cap(self):
        """The annotators and the model must see the same, fixed-length text; the cap is what makes 'what the
        model saw' a known quantity."""
        rows = lb.fetch_issues([("o/r", 1)], "2026-09-15T00:00:00Z", 100, 1, set(),
                               gh=fake_gh([issue(1, title="  Title  ", body="b" * 500)]))
        self.assertEqual(rows[0]["text"], "Title\n\n" + "b" * 93)

    def test_same_seed_same_draw_and_the_quota_is_respected(self):
        pages = [issue(n) for n in range(1, 41)]
        a = lb.fetch_issues([("o/r", 7)], "2026-09-15T00:00:00Z", 700, 5, set(), gh=fake_gh(pages))
        b = lb.fetch_issues([("o/r", 7)], "2026-09-15T00:00:00Z", 700, 5, set(), gh=fake_gh(pages))
        c = lb.fetch_issues([("o/r", 7)], "2026-09-15T00:00:00Z", 700, 6, set(), gh=fake_gh(pages))
        self.assertEqual(len(a), 7)
        self.assertEqual([r["id"] for r in a], [r["id"] for r in b])
        self.assertNotEqual([r["id"] for r in a], [r["id"] for r in c])
        self.assertNotEqual({r["id"] for r in a}, {r["id"] for r in c})      # another seed picks other issues, not just another order

    def test_the_parameters_are_read_the_way_they_are_written(self):
        self.assertEqual(lb.parse_quota("a/b=60,c/d=5"), [("a/b", 60), ("c/d", 5)])
        self.assertEqual(lb.parse_plan("a/b:usage:2026-05-01:30,c/d:good first issue:2025-01-01:20"),
                         [("a/b", "usage", "2026-05-01", 30), ("c/d", "good first issue", "2025-01-01", 20)])

    def test_usage_draw_marks_its_strata_skips_seen_ids_and_carries_old_rows(self):
        """The second draw is stratified on purpose; the stratum is how a reader later knows the rows are not a
        random sample. A row already in the first draw must not be labelled twice."""
        hits = [{"number": 1, "title": "t", "body": BODY, "html_url": "u1", "created_at": "x", "user": "User", "labels": ["usage"]},
                {"number": 2, "title": "t", "body": BODY, "html_url": "u2", "created_at": "x", "user": "Bot", "labels": []},
                {"number": 3, "title": "t", "body": BODY, "html_url": "u3", "created_at": "x", "user": "User", "labels": []}]
        rows = lb.fetch_usage([("o/r", "usage", "2026-01-01", 10)], 700, 1, {"r-3"},
                              [{"id": "r-9", "text": "old\r\ntext", "label": "bug-report"}, {"id": "r-1", "text": "dup", "label": None}],
                              "o/r", "carried", gh=lambda args: json.dumps(hits))
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(sorted(r["id"] for r in rows), ["r-1", "r-9"])      # a list, not the dict: a repeated id must show
        self.assertEqual(by_id["r-1"]["stratum"], "usage-labelled")
        self.assertEqual(by_id["r-9"]["stratum"], "carried")
        self.assertEqual(by_id["r-9"]["text"], "old\ntext")
        self.assertEqual(by_id["r-9"]["number"], 9)


class TestBlind(unittest.TestCase):
    def rows(self, n):
        return [{"id": f"r-{i}", "text": f"text {i}", "repo": "o/r", "number": i, "url": f"u{i}", "gh_labels": ["bug"]} for i in range(n)]

    def test_annotators_see_an_opaque_id_and_the_text_only(self):
        """If the annotator can see the repo, the issue number or the maintainers' label, the reference answer is
        no longer independent of those clues."""
        work = tmp(self)
        mapping = lb.make_blind(self.rows(5), work, chunk=2)
        for n in (1, 2, 3):
            for r in lb.read_jsonl(os.path.join(work, "annot", f"chunk{n}.jsonl")):
                self.assertEqual(sorted(r), ["id", "text"])
        self.assertEqual(mapping, {"c0001": "r-0", "c0002": "r-1", "c0003": "r-2", "c0004": "r-3", "c0005": "r-4"})
        with open(os.path.join(work, "mapping.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), mapping)

    def test_a_second_pool_continues_the_numbering_and_the_chunk_files(self):
        """Ids and chunk names must not collide with the first pool's, or merge would mix up two rows."""
        work = tmp(self)
        lb.make_blind(self.rows(5), work, chunk=2)
        try:
            second = lb.make_blind(self.rows(3), work, chunk=2, id_start=5, first_chunk=4, mapping_name="mapping2.json")
        except SystemExit as e:
            self.fail(f"the second pool was refused: {e}")
        self.assertEqual(sorted(second), ["c0006", "c0007", "c0008"])
        self.assertEqual(sorted(os.listdir(os.path.join(work, "annot"))), [f"chunk{n}.jsonl" for n in range(1, 6)])

    def test_a_file_that_already_exists_with_other_content_is_not_overwritten(self):
        """A wrong --id-start or --first-chunk would otherwise rewrite the input an annotator already received, and the
        results already collected would then be matched to different text."""
        work = tmp(self)
        lb.make_blind(self.rows(5), work, chunk=2)
        other = self.rows(5)
        other[0]["text"] = "changed"
        with self.assertRaises(SystemExit):
            lb.make_blind(other, work, chunk=2)
        lb.make_blind(self.rows(5), work, chunk=2)                  # the same data again is harmless
        lb.make_blind(other, work, chunk=2, force=True)             # and an explicit --force is allowed
        self.assertEqual(lb.read_jsonl(os.path.join(work, "annot", "chunk1.jsonl"))[0]["text"], "changed")

    def test_prompt_files_name_the_chunk_they_belong_to(self):
        work = tmp(self)
        lb.make_blind(self.rows(3), work, chunk=2)
        base = os.path.join(work, "p.md")
        lb.write_text(base, "INSTRUCTIONS\n")
        lb.write_prompts(work, base)
        with open(os.path.join(work, "prompts", "codex_chunk2.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("INSTRUCTIONS\n"))
        self.assertIn("`chunk2.jsonl`", text)


class TestMerge(unittest.TestCase):
    def setup_work(self, a_labels, b_labels, uncertain_a=(), uncertain_b=()):
        """Four rows in one pool, one chunk. a_/b_labels are the two annotators' labels in order."""
        work = tmp(self)
        rows = [{"id": f"r-{i}", "text": f"text {i}", "repo": "o/r", "number": i, "url": f"u{i}", "gh_labels": []} for i in range(4)]
        lb.write_jsonl(os.path.join(work, "candidates.jsonl"), rows)
        lb.make_blind(rows, work, chunk=60)
        ids = [f"c{i:04d}" for i in range(1, 5)]
        a = [{"id": c, "label": l, "uncertain": c in uncertain_a} for c, l in zip(ids, a_labels)]
        b = [{"id": c, "label": l, "uncertain": c in uncertain_b} for c, l in zip(ids, b_labels)]
        lb.write_text(os.path.join(work, "results_codex", "out1.md"), "thinking\n```json\n" + json.dumps(a) + "\n```\n")
        lb.write_text(os.path.join(work, "results_claude", "chunk1.json"), json.dumps(b))
        return work

    def run_merge(self, work):
        return lb.merge(work, [("candidates.jsonl", "random-draw")], ["mapping.json"])

    def test_agreement_numbers_match_a_hand_computation(self):
        """A: bug,bug,none,usage  B: bug,none,none,usage -> raw 3/4; chance = .5*.25+.25*.5+.25*.25 = .3125;
        kappa = (.75-.3125)/(1-.3125) = 0.6364. Any-rule-vs-none: A yes,yes,no,yes  B yes,no,no,yes -> 3/4, chance
        = .75*.5+.25*.5 = .5, kappa .5."""
        rep = self.run_merge(self.setup_work(["bug-report", "bug-report", None, "usage-question"],
                                             ["bug-report", None, None, "usage-question"]))
        self.assertEqual((rep["rows"], rep["agreed"], rep["disputed"]), (4, 3, 1))
        self.assertEqual(rep["raw_agreement_3class"], 0.75)
        self.assertEqual(rep["kappa_3class"], 0.6364)
        self.assertEqual(rep["kappa_any_rule_vs_none"], 0.5)

    def test_a_row_either_annotator_doubted_is_never_scored(self):
        """Same label but one of them said 'uncertain' is exactly the row whose reference answer is weakest."""
        work = self.setup_work(["bug-report"] * 4, ["bug-report"] * 4, uncertain_b=("c0002",))
        rep = self.run_merge(work)
        self.assertEqual((rep["agreed"], rep["disputed"]), (3, 1))
        agreed = [r["id"] for r in lb.read_jsonl(os.path.join(work, "labelled_agreed.jsonl"))]
        self.assertNotIn("r-1", agreed)
        self.assertEqual(lb.read_jsonl(os.path.join(work, "disputed.jsonl"))[0]["id"], "r-1")

    def test_pool_stratum_is_the_default_but_a_rows_own_stratum_wins(self):
        work = self.setup_work(["bug-report"] * 4, ["bug-report"] * 4)
        rows = lb.read_jsonl(os.path.join(work, "candidates.jsonl"))
        rows[0]["stratum"] = "special"
        lb.write_jsonl(os.path.join(work, "candidates.jsonl"), rows)
        rep = self.run_merge(work)
        self.assertEqual(sorted(rep["agreement_by_stratum"]), ["random-draw", "special"])
        self.assertEqual(rep["agreement_by_stratum"]["special"]["rows"], 1)
        self.assertEqual(rep["agreement_by_stratum"]["random-draw"]["rows"], 3)

    def test_an_annotator_file_that_is_wrong_in_any_way_is_refused(self):
        """Out-of-order ids, a label that is not a rule, a non-boolean `uncertain` and a missing JSON block would
        each silently shift labels onto the wrong rows if accepted."""
        good = ["bug-report"] * 4
        w = self.setup_work(good, good)
        with open(os.path.join(w, "results_claude", "chunk1.json"), encoding="utf-8") as f:
            items = json.load(f)
        cases = {
            "ids out of order": [items[1], items[0]] + items[2:],
            "unknown label": [dict(items[0], label="feature")] + items[1:],
            "uncertain not a boolean": [dict(items[0], uncertain="no")] + items[1:],
            "one row missing": items[:3],
        }
        for name, bad in cases.items():
            lb.write_text(os.path.join(w, "results_claude", "chunk1.json"), json.dumps(bad))
            with self.assertRaises(SystemExit, msg=name):
                self.run_merge(w)
        lb.write_text(os.path.join(w, "results_claude", "chunk1.json"), json.dumps(items))
        lb.write_text(os.path.join(w, "results_codex", "out1.md"), "no json here")
        with self.assertRaises(SystemExit):
            self.run_merge(w)

    def test_two_batches_that_reuse_blind_ids_are_refused(self):
        """Merged by dict update, the later batch would silently replace the earlier one and the report would just
        have fewer rows."""
        work = self.setup_work(["bug-report"] * 4, ["bug-report"] * 4)
        second = [{"id": "r-10", "text": "second", "repo": "o/r", "number": 10, "url": "u", "gh_labels": []}]
        lb.write_jsonl(os.path.join(work, "candidates2.jsonl"), second)
        lb.make_blind(second, work, id_start=0, first_chunk=2, mapping_name="mapping2.json")      # ids restart at c0001: wrong
        refused(self, lambda: lb.merge(work, [("candidates.jsonl", "random-draw"), ("candidates2.jsonl", None)],
                                       ["mapping.json", "mapping2.json"]), "reused blind ids")

    def test_a_blind_id_in_two_chunks_is_refused(self):
        """Each chunk can look right on its own; the same id in two of them would be counted once."""
        work = self.setup_work(["bug-report"] * 4, ["bug-report"] * 4)
        lb.write_jsonl(os.path.join(work, "annot", "chunk2.jsonl"), [{"id": "c0001", "text": "text 0"}])
        item = [{"id": "c0001", "label": "bug-report", "uncertain": False}]
        lb.write_text(os.path.join(work, "results_codex", "out2.md"), "```json\n" + json.dumps(item) + "\n```")
        lb.write_text(os.path.join(work, "results_claude", "chunk2.json"), json.dumps(item))
        with self.assertRaises(SystemExit):
            self.run_merge(work)

    def test_text_that_changed_after_blinding_is_refused(self):
        """The label belongs to the text the annotators read. If the candidate file was edited or rebuilt since,
        the label would be attached to a different message and nothing would show it."""
        work = self.setup_work(["bug-report"] * 4, ["bug-report"] * 4)
        rows = lb.read_jsonl(os.path.join(work, "candidates.jsonl"))
        rows[2]["text"] = "not what the annotators saw"
        lb.write_jsonl(os.path.join(work, "candidates.jsonl"), rows)
        with self.assertRaises(SystemExit):
            self.run_merge(work)

    def test_a_mapping_that_names_no_known_issue_or_one_issue_twice_is_refused(self):
        work = self.setup_work(["bug-report"] * 4, ["bug-report"] * 4)
        mapping_path = os.path.join(work, "mapping.json")
        with open(mapping_path, encoding="utf-8") as f:
            good = json.load(f)
        lb.write_text(mapping_path, json.dumps(dict(good, c0002="r-99")))
        with self.assertRaises(SystemExit):
            self.run_merge(work)
        rows = lb.read_jsonl(os.path.join(work, "candidates.jsonl"))
        for r in rows:
            r["text"] = "same text"                      # so only the 'one issue twice' check can object
        lb.write_jsonl(os.path.join(work, "candidates.jsonl"), rows)
        for name in ("annot",):
            chunk = os.path.join(work, name, "chunk1.jsonl")
            lb.write_jsonl(chunk, [dict(r, text="same text") for r in lb.read_jsonl(chunk)])
        lb.write_text(mapping_path, json.dumps(dict(good, c0002="r-0")))
        with self.assertRaises(SystemExit):
            self.run_merge(work)

    def test_the_same_inputs_give_byte_identical_outputs(self):
        """The point of the script: re-running it must not change the reference answer."""
        work = self.setup_work(["bug-report", None, None, "usage-question"], ["bug-report", None, "bug-report", "usage-question"])
        self.run_merge(work)
        names = ["agreement.json", "labelled_agreed.jsonl", "disputed.jsonl", "labelled_codex_all.jsonl", "labelled_claude_all.jsonl"]
        first = {n: open(os.path.join(work, n), "rb").read() for n in names}
        self.run_merge(work)
        self.assertEqual(first, {n: open(os.path.join(work, n), "rb").read() for n in names})


class TestCommandLine(unittest.TestCase):
    def test_fetch_writes_the_draw_and_its_sha256(self):
        """The draw depends on what GitHub holds today; the saved file and its hash are what make every later
        stage repeatable."""
        work = tmp(self)
        original = lb.run_gh
        lb.run_gh = fake_gh([issue(n) for n in range(1, 5)])
        self.addCleanup(setattr, lb, "run_gh", original)
        lb.main(["fetch", "--work-dir", work, "--repos", "o/r=3", "--since", "2026-09-15T00:00:00Z", "--seed", "1"])
        path = os.path.join(work, "candidates.jsonl")
        self.assertEqual(len(lb.read_jsonl(path)), 3)
        with open(path + ".sha256", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), lb.sha256_file(path))


if __name__ == "__main__":
    unittest.main()
