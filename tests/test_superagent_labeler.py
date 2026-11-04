"""The offline labelling page and its reader (Python side). The page's own behaviour (clicking, keys,
downloads) is exercised in a real browser by hand; what can be pinned without one is pinned here.

Each test's docstring says WHY the behaviour matters.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import superagent_support as sup  # noqa: F401
from superagent import corpus, labeler
from superagent.gate import Rule

RULES = [Rule("a", "the first rule"), Rule("b", "the second rule")]


class Files(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, text, name=None):
        self.n = getattr(self, "n", 0) + 1                 # a new file every time: tests build several up front
        path = os.path.join(self.dir, name or f"in{self.n}.txt")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return path

    def jsonl(self, *objs):
        return self.write("\n".join(json.dumps(o, ensure_ascii=False) for o in objs) + "\n")


class TestReader(Files):
    def test_plain_lines_become_rows_with_ids_and_defaults(self):
        rows = labeler.read_unlabelled(self.write("first message\n\nsecond one\n"), 1000)
        self.assertEqual([(r["id"], r["text"], r["sender"], r["thread"]) for r in rows],
                         [("c0001", "first message", "labeler", "t-c0001"),
                          ("c0002", "second one", "labeler", "t-c0002")])
        self.assertTrue(all("label" not in r for r in rows), "an unlabelled row has no label key at all")

    def test_ids_given_in_the_file_are_kept_and_generated_ones_do_not_collide(self):
        rows = labeler.read_unlabelled(self.jsonl({"id": "c0001", "text": "x"}, {"text": "y"}, {"id": "mine", "text": "z"}), 1000)
        self.assertEqual([r["id"] for r in rows], ["c0001", "c0002", "mine"])

    def test_bad_files_are_refused_with_every_problem(self):
        for body, needle in ((self.write(""), "no messages"),
                             (self.jsonl({"id": "a", "text": "x"}, {"id": "a", "text": "y"}), "used twice"),
                             (self.jsonl({"id": "a"}), "no text"),
                             (self.jsonl({"id": 5, "text": "x"}), "non-empty string"),
                             (self.jsonl({"id": "a", "text": "x", "truncated_chars": -1}), "truncated_chars"),
                             (self.write("{not json\n"), "not valid JSON")):
            with self.subTest(needle=needle):
                with self.assertRaises(labeler.LabelerError) as cm:
                    labeler.read_unlabelled(body, 1000)
                self.assertIn(needle, str(cm.exception))

    def test_the_text_is_cut_to_what_the_model_sees_and_the_cut_is_recorded(self):
        """A person who labels more than the model reads labels text the model never saw."""
        rows = labeler.read_unlabelled(self.jsonl({"id": "a", "text": "x" * 25},
                                                  {"id": "b", "text": "y" * 10, "truncated_chars": 7}), 10)
        self.assertEqual([(len(r["text"]), r["truncated_chars"], r["card_cap"]) for r in rows], [(10, 15, 10), (10, 7, 10)])

    def test_labels_already_in_the_file_are_kept_so_work_can_be_resumed(self):
        rows = labeler.read_unlabelled(self.jsonl({"id": "a", "text": "x", "label": "a"},
                                                  {"id": "b", "text": "y", "label": None},
                                                  {"id": "c", "text": "z", "uncertain": True},
                                                  {"id": "d", "text": "w"}), 1000)
        self.assertEqual([(r.get("label", "<none>"), r["uncertain"]) for r in rows],
                         [("a", False), (None, False), ("<none>", True), ("<none>", False)])

    def test_a_blind_export_is_accepted_as_is(self):
        """The file `decisions --for-labeling` writes is the intended input."""
        line = {"schema": 1, "id": "c0001", "source": "file:inbox", "thread": "t1", "sender": "ann", "text": "hello",
                "truncated_chars": 3, "card_cap": 1000, "uncertain": False}
        row, = labeler.read_unlabelled(self.jsonl(line), 1000)
        self.assertEqual((row["id"], row["sender"], row["thread"], row["truncated_chars"]), ("c0001", "ann", "t1", 3))


class TestPage(Files):
    TRICKS = "</script><script>alert(1)</script> <b>bold</b>   ‮ rtl & \"quotes\""

    def page(self, text=None, title="標註 <訊息>"):
        rows = labeler.read_unlabelled(self.jsonl({"id": "a", "text": text or self.TRICKS}), 1000)
        return labeler.build_html(rows, RULES, title=title), rows

    def data_of(self, html):
        m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)
        return json.loads(m.group(1))

    def test_the_data_survives_the_trip_into_the_page_exactly(self):
        """A message that contains </script> must not end the data block, and must come back unchanged."""
        html, rows = self.page()
        data = self.data_of(html)
        self.assertEqual(data["rows"][0]["text"], self.TRICKS)
        self.assertEqual([r["name"] for r in data["rules"]], ["a", "b"])
        self.assertEqual(html.count("<script"), 2, "only the data block and the code block are scripts")
        block = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1)
        self.assertNotIn("<", block, "no raw '<' in the data block, so no '<!--<script>' trick either")
        self.assertNotIn(" ", html)

    def test_the_page_is_offline_and_never_writes_message_text_as_html(self):
        """Offline, so a message cannot make the page fetch anything; textContent, so it cannot become markup."""
        html, _ = self.page()
        self.assertNotRegex(html, r"https?://")
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("outerHTML", html)
        self.assertNotIn("insertAdjacentHTML", html)
        self.assertNotRegex(html, r"\bsrc=|\bhref=\"http|@import")

    def test_saved_answers_are_keyed_by_content_not_by_ids(self):
        """The blind export always numbers its rows c0001, c0002 ...; a second batch of the same size would
        otherwise pick up the first batch's saved answers in the browser."""
        a = self.data_of(labeler.build_html(labeler.read_unlabelled(self.jsonl({"id": "c0001", "text": "one"}), 1000), RULES))
        a2 = self.data_of(labeler.build_html(labeler.read_unlabelled(self.jsonl({"id": "c0001", "text": "one"}), 1000), RULES))
        b = self.data_of(labeler.build_html(labeler.read_unlabelled(self.jsonl({"id": "c0001", "text": "other"}), 1000), RULES))
        c = self.data_of(labeler.build_html(labeler.read_unlabelled(self.jsonl({"id": "c0001", "text": "one"}), 1000), RULES[:1]))
        self.assertEqual(a["fingerprint"], a2["fingerprint"])
        self.assertNotEqual(a["fingerprint"], b["fingerprint"])
        self.assertNotEqual(a["fingerprint"], c["fingerprint"], "other rules, other job")

    def test_the_title_is_escaped(self):
        html, _ = self.page()
        self.assertIn("<title>標註 &lt;訊息&gt;</title>", html)

    def test_nothing_the_model_said_can_be_in_the_page(self):
        """The page is built from the rows of the input file only; a model's verdict has no place in it."""
        row = {"id": "a", "text": "x", "verdict": "ignore", "model_rule": "SECRET-RULE", "reason": "SECRET-REASON"}
        rows = labeler.read_unlabelled(self.jsonl(row), 1000)
        html = labeler.build_html(rows, RULES)
        for leak in ("SECRET-RULE", "SECRET-REASON", "verdict", "model_rule"):
            self.assertNotIn(leak, html)

    def test_a_labelled_file_the_page_would_download_loads_in_eval(self):
        """Contract between the page's download and the corpus loader, written out by hand here the way
        the page's JavaScript writes it (checked against the real page in a browser)."""
        rows = labeler.read_unlabelled(self.write("first\nsecond\nthird\n"), 1000)
        out = []
        for r, (label, unsure) in zip(rows, (("a", False), (None, False), (None, True))):
            o = {"id": r["id"], "text": r["text"], "sender": r["sender"], "thread": r["thread"], "card_cap": r["card_cap"],
                 "label": label, "uncertain": unsure}
            out.append(json.dumps(o))
        path = self.write("\n".join(out) + "\n", "labelled.jsonl")
        got = corpus.load(path, RULES, 1000)
        self.assertEqual([(r.label, r.uncertain) for r in got.rows], [("a", False), (None, False), (None, True)])


NODE = shutil.which("node")
HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "labeler_page_harness.js")


@unittest.skipIf(NODE is None, "node is not installed: the page's own script is not exercised")
class TestPageScriptInNode(Files):
    """The page's real JavaScript, run in Node against a minimal fake DOM (tests/labeler_page_harness.js).
    Before this, only the Python side was tested and the page was checked by hand in a browser."""

    def run_page(self, objs, actions, rules=RULES, **opts):
        rows = labeler.read_unlabelled(self.jsonl(*objs), 1000)
        html_path = self.write(labeler.build_html(rows, rules), f"page{self.n + 1}.html")
        done = subprocess.run([NODE, HARNESS, html_path, json.dumps(dict(opts, actions=actions))],
                              capture_output=True, text=True, encoding="utf-8", timeout=60,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def lines(self, download):
        return [json.loads(x) for x in download["text"].split("\n") if x]

    def test_keys_label_rows_and_the_two_downloads_differ_as_documented(self):
        out = self.run_page([{"id": "a", "text": "one"}, {"id": "b", "text": "two"}, {"id": "c", "text": "three"}],
                            ["key:1", "key:u", "click:dlLabelled", "click:dlAll"])
        self.assertEqual([s["progress"] for s in out["steps"]], ["已標 0 / 3", "已標 1 / 3", "已標 2 / 3", "已標 2 / 3", "已標 2 / 3"])
        labelled, everything = (self.lines(d) for d in out["downloads"])
        self.assertEqual([(r["id"], r["label"], r["uncertain"]) for r in labelled], [("a", "a", False), ("b", None, True)])
        self.assertEqual([("label" in r) for r in everything], [True, True, False], "an unlabelled row has no label key")
        self.assertEqual(out["downloads"][0]["name"], "corpus_labelled.jsonl")

    def test_ids_that_are_javascript_property_names_are_ordinary_ids(self):
        """Second review: answers was a plain object, so 'constructor' looked answered from the start."""
        ids = ["constructor", "__proto__", "toString", "hasOwnProperty", "valueOf"]
        out = self.run_page([{"id": i, "text": f"m {i}"} for i in ids], ["key:1", "click:dlLabelled"])
        self.assertEqual(out["steps"][0]["progress"], "已標 0 / 5")
        self.assertEqual(out["steps"][1]["progress"], "已標 1 / 5")
        self.assertEqual([r["id"] for r in self.lines(out["downloads"][0])], ["constructor"])
        out = self.run_page([{"id": i, "text": f"m {i}"} for i in ids], ["click:nextOpen"])
        self.assertIn("__proto__", out["steps"][1]["meta"], "the next open row is the second one, not skipped")

    def test_the_downloaded_text_is_the_original_and_the_screen_text_is_spelled(self):
        nasty = "</script><b>x</b>‮ rtl sep\U000E0041 tag‍zwj\nline2\ttab"
        out = self.run_page([{"id": "n", "text": nasty}], ["key:0", "click:dlLabelled"])
        shown = out["steps"][0]["text"]
        for ch in ("‮", " ", "\U000E0041"):
            self.assertNotIn(ch, shown)
        self.assertIn("\\u202e", shown)
        self.assertIn("\\udb40\\udc41", shown, "an astral character is spelled as its surrogate pair, as sanitize.visible does")
        self.assertIn("‍", shown, "ZWJ is kept")
        self.assertEqual(self.lines(out["downloads"][0])[0]["text"], nasty)

    def test_what_the_page_downloads_loads_in_eval_even_with_separators_inside_a_message(self):
        out = self.run_page([{"id": "n", "text": "a b"}, {"id": "m", "text": "plain"}], ["key:1", "key:0", "click:dlLabelled"])
        path = self.write(out["downloads"][0]["text"])
        got = corpus.load(path, RULES, 1000)
        self.assertEqual([(r.id, r.text, r.label) for r in got.rows], [("n", "a b", "a"), ("m", "plain", None)])

    def test_navigation_clear_and_the_jump_to_the_next_open_row(self):
        objs = [{"id": "a", "text": "1"}, {"id": "b", "text": "2"}, {"id": "c", "text": "3"}]
        out = self.run_page(objs, ["key:ArrowLeft", "key:1", "key:ArrowLeft", "key:Backspace", "click:nextOpen"])
        self.assertEqual(out["steps"][1]["meta"].split("　")[1], "a", "left from the first row stays on it")
        self.assertEqual(out["steps"][2]["progress"], "已標 1 / 3")
        self.assertEqual(out["steps"][4]["progress"], "已標 0 / 3", "Backspace clears the answer of the shown row")
        self.assertEqual(out["steps"][5]["meta"].split("　")[1], "b")

    def test_keys_stop_at_nine_rules_and_no_key_is_advertised_beyond_them(self):
        """Second review: the tenth rule's button said [10] but the key pressed chose rule 1."""
        rules = [Rule(f"r{i}", f"rule {i}") for i in range(1, 12)]
        out = self.run_page([{"id": "a", "text": "x"}], ["button:9", "click:dlLabelled"], rules=rules)
        buttons = [b["text"] for b in out["steps"][0]["buttons"]]
        self.assertTrue(buttons[0].startswith("[1] r1"))
        self.assertTrue(buttons[8].startswith("[9] r9"))
        self.assertTrue(buttons[9].startswith("r10") and "[" not in buttons[9].split("rule")[0], buttons[9])
        self.assertEqual(self.lines(out["downloads"][0])[0]["label"], "r10", "the mouse still reaches it")

    def test_answers_already_in_the_file_are_restored_and_shown_as_chosen(self):
        objs = [{"id": "a", "text": "1", "label": "b"}, {"id": "b", "text": "2", "label": None},
                {"id": "c", "text": "3", "uncertain": True}, {"id": "d", "text": "4"}]
        out = self.run_page(objs, ["click:nextOpen", "click:dlAll"])
        self.assertEqual(out["steps"][0]["progress"], "已標 3 / 4")
        self.assertTrue(out["steps"][0]["buttons"][1]["on"], "the 'b' button is shown as chosen")
        self.assertEqual(out["steps"][1]["meta"].split("　")[1], "d")
        self.assertEqual([("label" in r, r.get("uncertain")) for r in self.lines(out["downloads"][0])],
                         [(True, False), (True, False), (True, True), (False, None)])

    def test_a_row_marked_unsure_never_downloads_with_a_rule(self):
        """Mutation check found this guard unprotected: a file that has both a label and uncertain:true (an
        older download, or one edited by hand) is shown as 'unsure' and must come back as unsure with no
        label, not as a labelled row that happens to be flagged."""
        out = self.run_page([{"id": "a", "text": "1", "label": "a", "uncertain": True}], ["click:dlAll"])
        self.assertTrue(out["steps"][0]["buttons"][-1]["on"], "the 'unsure' button is the chosen one")
        row = self.lines(out["downloads"][0])[0]
        self.assertEqual((row["label"], row["uncertain"]), (None, True))

    def test_browser_storage_saves_resumes_and_may_be_missing(self):
        objs = [{"id": "a", "text": "1"}, {"id": "b", "text": "2"}, {"id": "c", "text": "3"}]
        first = self.run_page(objs, ["key:2", "key:2"])
        again = self.run_page(objs, [], saved=first["saved"])
        self.assertEqual(again["steps"][0]["progress"], "已標 2 / 3")
        self.assertIn("b", again["steps"][0]["meta"].split("　")[1], "the cursor comes back too")
        broken = self.run_page(objs, ["key:1", "click:dlLabelled"], storage="throw")
        self.assertIn("不讓暫存", broken["steps"][1]["saved"])
        self.assertEqual(len(self.lines(broken["downloads"][0])), 1, "the downloads work without storage")

    def test_the_storage_key_follows_the_content(self):
        a = self.run_page([{"id": "c0001", "text": "one"}], ["key:1"])["key"]
        b = self.run_page([{"id": "c0001", "text": "another"}], ["key:1"])["key"]
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("superagent-labeler:"))


if __name__ == "__main__":
    unittest.main()
