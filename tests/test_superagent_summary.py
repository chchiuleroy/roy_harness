"""The readable text version of a report (`report` command and the summary `eval --out` prints).

Each test's docstring says WHY the behaviour matters.
"""
import json
import os
import unittest

from superagent import summary
from superagent import replay as rp
from superagent.measure import NULL
from test_superagent_replay import HAND, HAND_SAYS, ReplayCase, RULES


class SummaryCase(ReplayCase):
    def report(self, **kw):
        self.script(lambda text, n: HAND_SAYS[text])
        return rp.replay(self.corpus(*HAND), self.cfg(batch_max=4, **kw))


class TestRender(SummaryCase):
    def test_the_numbers_on_screen_are_the_numbers_in_the_report(self):
        """The text is a view of the report, not a second calculation: the hand-worked table's counts must
        be on the screen as they are in the JSON."""
        text = summary.render(self.report())
        for line in ("3/5", "3/4", "2/4", "7/9", "2/3"):
            self.assertIn(line, text)
        self.assertIn("60.0%", text)                                    # recall 3/5
        self.assertIn("區間", text)
        self.assertIn("unknown_rule 1", text)   # a failure kind is listed
        self.assertIn("no acceptance threshold has been set", text)     # the report's own closing sentence

    def test_it_never_calls_the_model_good_or_bad(self):
        text = summary.render(self.report(), errors=True)
        self.assertNotRegex(text, r"(?i)\bpassed\b|\bfailed\b|\bacceptable\b|通過|不合格|合格")

    def test_no_message_text_unless_the_caller_asks_for_errors_and_gives_the_corpus(self):
        rep = self.report()
        texts = summary.snippets(self.path)
        self.assertNotIn("text 2", summary.render(rep))
        self.assertNotIn("text 2", summary.render(rep, errors=True))               # errors but no corpus: ids only
        self.assertIn("text 2", summary.render(rep, errors=True, texts=texts))

    def test_the_wrong_rows_are_exactly_the_scored_rows_whose_first_answer_differs(self):
        """HAND_SAYS: rows 2 (a->b), 3 (a->null), 4 (omitted), 5 right, 8 (unknown), 7 (null->a)."""
        rep = self.report()
        text = summary.render(rep, errors=True)
        wrong = [ln.split()[0] for ln in text.splitlines() if ln.startswith("  m")]
        self.assertEqual(sorted(wrong), ["m2", "m3", "m4", "m7", "m8"])
        self.assertIn("答錯或沒答案的計分列（第 1 次）：5", text)

    def test_the_error_list_follows_the_first_repeat_like_the_main_figures(self):
        """Mutation check found this: with several repeats the figures use the first one, so the rows
        listed as wrong must be the first one's, not the last one's."""
        rep = self.report()
        by_id = {r["id"]: r for r in rep["rows"]}
        by_id["m1"]["outcomes"] = ["b", "a"]        # wrong first, right later: listed
        by_id["m5"]["outcomes"] = ["b", "null"]     # right first (label b), wrong later: not listed
        listed = [ln.split()[0] for ln in summary.render(rep, errors=True).splitlines() if ln.startswith("  m")]
        self.assertIn("m1", listed)
        self.assertNotIn("m5", listed)

    def test_uncertain_and_cut_rows_are_not_listed_as_errors(self):
        self.script(lambda text, n: NULL)
        rep = rp.replay(self.corpus(dict(HAND[0], uncertain=True), dict(HAND[5]), dict(HAND[1], text="x" * 1200),
                                    allow_truncated=True), self.cfg())
        text = summary.render(rep, errors=True)
        self.assertIn("答錯或沒答案的計分列（第 1 次）：0", text)

    def test_stability_and_order_sections_say_what_they_cannot_say(self):
        text = summary.render(self.report())
        self.assertIn("穩定度：只跑了 1 次", text)
        self.assertNotIn("順序敏感度", text)
        rep = self.report(repeats=2, order_check=True)
        text = summary.render(rep)
        self.assertIn("穩定度：", text)
        self.assertIn("順序敏感度：", text)
        self.assertIn("不是位置效應的估計", text)

    def test_unsafe_characters_in_ids_and_the_model_name_are_spelled_not_shown(self):
        rep = self.report()
        rep["meta"]["model"] = "m‮odel"
        rep["rows"][0]["id"] = "i d"
        out = summary.render(rep, errors=True)
        for ch in ("‮", " "):
            self.assertNotIn(ch, out)
        self.assertIn("\\u202e", out)


class TestSnippets(SummaryCase):
    def test_layout_is_flattened_first_and_long_texts_are_cut(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"id": "a", "text": "first\n\nsecond\tthird " + "w" * 200}) + "\n")
            f.write("not json\n")
            f.write(json.dumps({"id": "b", "text": "evil ‮ text"}) + "\n")
            f.write(json.dumps({"id": 5, "text": "no"}) + "\n")
        got = summary.snippets(self.path)
        self.assertEqual(set(got), {"a", "b"})
        self.assertTrue(got["a"].startswith("first second third w"))
        self.assertTrue(got["a"].endswith("…"))
        self.assertLessEqual(len(got["a"]), summary.SNIPPET + 1)
        self.assertEqual(got["b"], "evil \\u202e text")


if __name__ == "__main__":
    unittest.main()
