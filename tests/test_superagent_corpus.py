"""Slice 4, S2: the labelled corpus is checked hard, and every problem is reported at once.

Each test's docstring says WHY the behaviour matters.
"""
import hashlib
import json
import os
import tempfile
import unittest

import superagent_support as sup  # noqa: F401
from superagent import corpus
from superagent.corpus import CorpusError

RULES = ["export-bug", "billing"]
CAP = 40


class CorpusCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "corpus.jsonl")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, *items, raw=None):
        with open(self.path, "wb") as f:
            if raw is not None:
                f.write(raw)
            else:
                f.write(("\n".join(i if isinstance(i, str) else json.dumps(i, ensure_ascii=False) for i in items)
                         + "\n").encode("utf-8"))

    def load(self, **kw):
        return corpus.load(self.path, RULES, CAP, **kw)

    def problems(self, **kw):
        with self.assertRaises(CorpusError) as cm:
            self.load(**kw)
        return cm.exception.problems


def row(i, label=None, **kw):
    return dict({"id": f"m{i}", "text": f"message {i}", "label": label}, **kw)


class TestAGoodCorpus(CorpusCase):
    def test_rows_get_their_defaults(self):
        self.write(row(1, "export-bug"), row(2, None, sender="ann", thread="t9", uncertain=True, note="hmm"))
        c = self.load()
        a, b = c.rows
        self.assertEqual((a.id, a.label, a.sender, a.thread, a.uncertain), ("m1", "export-bug", "labeler", "t-m1", False))
        self.assertEqual((b.sender, b.thread, b.uncertain, b.note), ("ann", "t9", True, "hmm"))

    def test_the_file_fingerprint_is_of_the_bytes(self):
        """So a report can say exactly which corpus it was about, and a changed label changes it."""
        self.write(row(1), row(2))
        with open(self.path, "rb") as f:
            want = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(self.load().sha256, want)

    def test_blank_lines_and_a_byte_order_mark_are_fine(self):
        self.write(raw=b"\xef\xbb\xbf" + json.dumps(row(1)).encode() + b"\n\n" + json.dumps(row(2)).encode() + b"\n")
        self.assertEqual([r.id for r in self.load().rows], ["m1", "m2"])

    def test_extra_fields_are_allowed(self):
        """A blind export carries a decision_id; the labeller's file may keep it."""
        self.write(dict(row(1), decision_id=17, source="file:inbox"))
        self.assertEqual(len(self.load().rows), 1)


class TestLineSeparators(CorpusCase):
    def test_unicode_line_separators_inside_a_message_do_not_split_the_row(self):
        """Found while building the labelling page: str.splitlines() also breaks at U+2028, U+2029, U+0085,
        form feed and others, and a browser's JSON.stringify writes them raw. A message containing one
        used to be cut in the middle of its JSON string and the file was refused (or worse, misread)."""
        text = "a b c\u0085d\x0ce\x1cf"
        self.write(row(1, text=text), row(2))
        got = self.load()
        self.assertEqual([r.id for r in got.rows], ["m1", "m2"])

    def test_windows_line_endings_are_fine(self):
        self.write(raw=b'{"id": "a", "text": "x", "label": null}\r\n{"id": "b", "text": "y", "label": null}\r\n')
        self.assertEqual(len(self.load().rows), 2)


class TestEveryProblemAtOnce(CorpusCase):
    def test_all_problems_are_listed_not_just_the_first(self):
        """A labeller fixing one error per run would give up."""
        self.write(row(1), row(1), {"id": "m3", "text": "x"}, row(4, "no-such-rule"), {"id": "", "text": "x", "label": None},
                   {"id": "m6", "text": "   ", "label": None}, "not json", "[1, 2]",
                   row(8, None, uncertain="yes"), row(9, None, sender=""))
        got = " | ".join(self.problems())
        for needle in ("already used", "label is missing", "no-such-rule", "id must be", "text must be",
                       "not valid JSON", "not a JSON object", "uncertain must be", "sender must be"):
            self.assertIn(needle, got)

    def test_an_empty_corpus_is_refused(self):
        self.write()
        self.assertIn("no rows", " ".join(self.problems()))

    def test_a_label_that_is_not_text_is_refused(self):
        self.write(dict(row(1), label=5))
        self.assertIn("neither null nor one of the rules", " ".join(self.problems()))


class TestTruncation(CorpusCase):
    def test_text_longer_than_the_model_sees_is_refused_by_default(self):
        """The model reads at most card_cap characters. A label about the rest is a label about text it never saw."""
        self.write(dict(row(1), text="x" * (CAP + 1)))
        self.assertIn("the model sees", " ".join(self.problems()))

    def test_exactly_at_the_cap_is_fine(self):
        self.write(dict(row(1), text="x" * CAP))
        self.assertEqual(self.load().rows[0].truncated_chars, 0)

    def test_when_allowed_the_row_says_how_much_is_cut(self):
        self.write(dict(row(1), text="x" * (CAP + 7)))
        self.assertEqual(self.load(allow_truncated=True).rows[0].truncated_chars, 7)


class TestThreads(CorpusCase):
    def test_two_rows_in_one_thread_are_refused_by_default(self):
        """Messages of one thread depend on each other; the intervals assume independent rows."""
        self.write(row(1, thread="t"), row(2, thread="t"), row(3, thread="u"))
        self.assertIn("share a thread", " ".join(self.problems()))

    def test_when_allowed_it_is_a_warning_with_the_count(self):
        self.write(row(1, thread="t"), row(2, thread="t"), row(3, thread="u"))
        c = self.load(allow_shared_threads=True)
        self.assertEqual(c.shared_thread_rows, 2)
        self.assertTrue(any("not independent" in w for w in c.warnings))


class TestChunks(unittest.TestCase):
    def test_consecutive_slices_of_at_most_batch_max(self):
        self.assertEqual([len(c) for c in corpus.chunks(range(5), 2)], [2, 2, 1])
        self.assertEqual([len(c) for c in corpus.chunks(range(4), 2)], [2, 2])
        self.assertEqual(corpus.chunks([], 3), [])
        self.assertEqual(corpus.chunks([1, 2, 3], 2), [[1, 2], [3]])

    def test_a_bad_batch_size_is_refused(self):
        for bad in (0, -1, 2.5, True, "3"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    corpus.chunks([1], bad)


if __name__ == "__main__":
    unittest.main()
