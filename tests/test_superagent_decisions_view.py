"""Slice 4, S3: reading the recorded decisions without touching the home, and exporting them for labelling.

Each test's docstring says WHY the behaviour matters.
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

import superagent_support as sup
from superagent import decisions_view as dv
from superagent.decisions_view import DecisionsError
from superagent.model import Decision, Strength, Trigger, Verdict
from superagent.store import Store

TRICKS = "a\u202eb\u009bc\u200bd\u2066e\ufeff\u2028f\x00gh"      # no lone surrogate: a database cannot hold one


def trig(ext, text="a message", sender="bob", thread=None):
    return Trigger("file:inbox", ext, thread or f"t-{ext}", sender, text, False, 1.0)


class Homes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self._tmp.name, "home")

    def tearDown(self):
        self._tmp.cleanup()

    def populate(self):
        """Five decisions: model said a rule (observe), model said none, no model answer (explicit request),
        a gave-up candidate, and a rule-based ignore. Triggers 1-4 went to the model."""
        with Store(self.home) as s:
            with s.tx():
                for i in range(1, 6):
                    s.add_trigger(trig(f"e{i}", text=f"text {i}", sender="ann" if i % 2 else "bob"))
                for i in (1, 2, 3, 4):
                    s.enqueue_eval(i)
            s.record_decision(1, Decision(Verdict.IGNORE, Strength.INFERRED, "observe: would accept (rule x): SECRET-REASON",
                                          would_accept=True, model_rule="x"))
            s.record_decision(2, Decision(Verdict.IGNORE, Strength.INFERRED, "model: chat", model_rule=""))
            s.record_decision(3, Decision(Verdict.UNAVAILABLE, Strength.INFERRED, "no usable answer after 3 tries"))
            s.record_decision(4, Decision(Verdict.UNAVAILABLE, Strength.INFERRED, "unknown rule", model_rule="made-up"))
            s.record_decision(5, Decision(Verdict.IGNORE, Strength.NOT_ADMITTED, "not admitted"))
            return s.db.execute("SELECT MIN(ts), MAX(ts) FROM decision").fetchone()

    def snapshot(self):
        """Every file under the home: name, size, mtime and content hash."""
        out = {}
        for dirpath, _, names in os.walk(self.home):
            for n in names:
                p = os.path.join(dirpath, n)
                st = os.stat(p)
                with open(p, "rb") as f:
                    out[os.path.relpath(p, self.home)] = (st.st_size, st.st_mtime_ns, hashlib.sha256(f.read()).hexdigest())
        return out


class TestReading(Homes):
    def test_rows_come_oldest_first_with_the_message_joined_in(self):
        self.populate()
        rows = dv.read_decisions(self.home)
        self.assertEqual([r["decision_id"] for r in rows], [1, 2, 3, 4, 5])
        self.assertEqual((rows[0]["sender"], rows[0]["text"], rows[0]["model_rule"], rows[0]["would_accept"]),
                         ("ann", "text 1", "x", True))
        self.assertEqual([r["model_rule"] for r in rows], ["x", "", None, "made-up", None])

    def test_the_filters(self):
        self.populate()
        ids = lambda **kw: [r["decision_id"] for r in dv.read_decisions(self.home, **kw)]       # noqa: E731
        self.assertEqual(ids(verdict="unavailable"), [3, 4])
        self.assertEqual(ids(would_accept=True), [1])
        self.assertEqual(ids(limit=2), [4, 5], "the latest two, still oldest first")
        self.assertEqual(ids(candidates_only=True), [1, 2, 3, 4], "the rule-based ignore never went to the model")
        self.assertEqual(ids(since=time.time() + 3600), [])
        self.assertEqual(ids(since=0), [1, 2, 3, 4, 5])

    def test_bad_arguments_are_refused(self):
        self.populate()
        for kw in ({"verdict": "approved"}, {"limit": 0}, {"limit": True}):
            with self.subTest(kw=kw):
                with self.assertRaises(DecisionsError):
                    dv.read_decisions(self.home, **kw)

    def test_since_takes_an_epoch_or_an_iso_time(self):
        self.assertEqual(dv.parse_since("1700000000"), 1700000000.0)
        self.assertEqual(dv.parse_since("2026-01-02T03:04:05+00:00"), 1767323045.0)
        with self.assertRaises(DecisionsError):
            dv.parse_since("yesterday")

    def test_a_home_that_never_ran_says_so(self):
        with self.assertRaises(DecisionsError) as cm:
            dv.read_decisions(os.path.join(self._tmp.name, "nothing"))
        self.assertIn("never been run", str(cm.exception))

    def test_a_home_made_before_the_column_existed_reads_with_none(self):
        """The reader must work on homes it has not been upgraded for, without opening them for writing."""
        self.populate()
        db = sqlite3.connect(os.path.join(self.home, "state.sqlite"))
        db.execute("ALTER TABLE decision DROP COLUMN model_rule")
        db.commit()
        db.close()
        rows = dv.read_decisions(self.home)
        self.assertEqual([r["model_rule"] for r in rows], [None] * 5)


class TestItNeverChangesTheHome(Homes):
    def test_every_file_is_byte_for_byte_and_mtime_for_mtime_the_same(self):
        """Read-only is a claim about the whole folder, not only state.sqlite: no sidecar may appear,
        none may change."""
        self.populate()
        before = self.snapshot()
        dv.read_decisions(self.home)
        dv.read_decisions(self.home, verdict="ignore", limit=2, candidates_only=True)
        dv.to_jsonl(dv.read_decisions(self.home), blind=True)
        self.assertEqual(self.snapshot(), before)

    def test_it_can_read_while_an_orchestrator_holds_the_home(self):
        """status works that way; so must this."""
        self.populate()
        with Store(self.home):
            self.assertEqual(len(dv.read_decisions(self.home)), 5)

    def test_a_write_ahead_log_home_is_refused_not_read(self):
        """A WAL database is kept in the file itself, so a reader could create -wal and -shm files. Only the
        journal mode this package uses is read."""
        self.populate()
        db = sqlite3.connect(os.path.join(self.home, "state.sqlite"))
        db.execute("PRAGMA journal_mode=WAL")
        db.close()
        before = self.snapshot()
        with self.assertRaises(DecisionsError) as cm:
            dv.read_decisions(self.home)
        self.assertIn("write-ahead-log", str(cm.exception))
        self.assertEqual(self.snapshot(), before)

    def test_a_real_hot_journal_is_reported_and_left_alone(self):
        """A writer that dies mid-transaction leaves a rollback journal a read-only connection cannot
        roll back. Made for real here (a child process that exits without committing) because a made-up
        file called -journal is not necessarily treated as hot by SQLite."""
        self.populate()
        path = os.path.join(self.home, "state.sqlite")
        code = textwrap.dedent("""
            import os, sqlite3, sys
            db = sqlite3.connect(sys.argv[1], isolation_level=None)
            db.execute('PRAGMA cache_size=10')      # a tiny cache, so the transaction spills to disk
            db.execute('BEGIN')
            for i in range(3000):
                db.execute("INSERT INTO trigger(source, ext_id, thread, sender, text, addressed, ts) "
                           "VALUES('s', ?, 't', 'x', ?, 0, 1.0)", ('crash%d' % i, 'y' * 300))
            os._exit(0)
        """)
        subprocess.run([sys.executable, "-X", "utf8", "-c", code, path], check=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        journal = path + "-journal"
        self.assertTrue(os.path.exists(journal), "the child did not leave a journal behind")
        before = self.snapshot()
        with self.assertRaises(DecisionsError) as cm:
            dv.read_decisions(self.home)
        self.assertIn("middle of a write", str(cm.exception))
        self.assertEqual(self.snapshot(), before, "the reader changed the folder")


class TestExport(Homes):
    def test_the_blind_export_has_exactly_the_fields_a_labeller_needs(self):
        """Anything about what the model said would steer the person labelling."""
        self.populate()
        lines = dv.to_jsonl(dv.read_decisions(self.home, candidates_only=True), blind=True).splitlines()
        self.assertEqual(len(lines), 4)
        for line in lines:
            obj = json.loads(line)
            self.assertEqual(set(obj), {"schema", "id", "source", "thread", "sender", "text",
                                        "truncated_chars", "card_cap", "uncertain"})
            self.assertEqual((obj["uncertain"], obj["schema"]), (False, 1))
        blob = "\n".join(lines)
        for leak in ("SECRET-REASON", "made-up", "verdict", "model_rule", "would_accept", "unavailable"):
            self.assertNotIn(leak, blob)

    def test_the_blind_text_is_what_the_model_saw_and_says_how_much_was_cut(self):
        """A 200-character export of a message the model read 1000 characters of would label text the
        model's answer was not about."""
        with Store(self.home) as s:
            for i, n in enumerate((10, 1000, 1001, 1500), 1):
                s.add_trigger(trig(f"e{i}", text="x" * n))
                s.record_decision(i, Decision(Verdict.IGNORE, Strength.INFERRED, "r", model_rule=""))
        got = [(len(o["text"]), o["truncated_chars"]) for o in
               map(json.loads, dv.to_jsonl(dv.read_decisions(self.home), blind=True).splitlines())]
        self.assertEqual(got, [(10, 0), (1000, 0), (1000, 1), (1000, 500)])
        got = [(len(o["text"]), o["text_truncated"]) for o in
               map(json.loads, dv.to_jsonl(dv.read_decisions(self.home), text_cap=200).splitlines())]
        self.assertEqual(got, [(10, False), (200, True), (200, True), (200, True)])

    def test_the_blind_ids_cannot_be_joined_to_the_answers(self):
        """The first review found that the database key in the labelling file is a join key back to the
        model's verdicts. The ids are opaque and numbered in file order."""
        self.populate()
        rows = dv.read_decisions(self.home, candidates_only=True)
        blind = [json.loads(x) for x in dv.to_jsonl(rows, blind=True).splitlines()]
        self.assertEqual([b["id"] for b in blind], ["c0001", "c0002", "c0003", "c0004"])
        blob = dv.to_jsonl(rows, blind=True)
        for key in ("decision_id", "trigger_id", "label"):
            self.assertNotIn(f'"{key}"', blob)

    def test_an_unlabelled_blind_row_is_refused_by_the_corpus_loader_not_counted_as_no_rule(self):
        """The export has no `label` key. If it said null, a row nobody labelled would silently be a
        'no rule applies' negative and raise specificity."""
        from superagent import corpus
        from superagent.gate import Rule
        self.populate()
        path = os.path.join(self._tmp.name, "blind.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(dv.to_jsonl(dv.read_decisions(self.home, candidates_only=True), blind=True))
        with self.assertRaises(corpus.CorpusError) as cm:
            corpus.load(path, (Rule("x", "an x rule"),), 1000)
        self.assertTrue(any("label is missing" in p for p in cm.exception.problems), cm.exception.problems)
        with open(path, encoding="utf-8") as f:
            labelled = [dict(json.loads(x), label=None) for x in f.read().splitlines()]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(o) for o in labelled) + "\n")
        self.assertEqual(len(corpus.load(path, (Rule("x", "an x rule"),), 1000, allow_shared_threads=True).rows), 4)

    def test_a_text_the_export_cut_stays_cut_when_the_file_is_loaded_back(self):
        """Second review: the export wrote truncated_chars, but the loader worked it out again from the
        (already cut) text, found nothing cut, and scored a row whose tail nobody labelled. Round trip:
        export -> label -> load must still know the row was cut."""
        from superagent import corpus
        from superagent.gate import Rule
        rules = (Rule("x", "an x rule"),)
        with Store(self.home) as s:
            for i, n in enumerate((10, 1500), 1):
                s.add_trigger(trig(f"e{i}", text="y" * n))
                s.enqueue_eval(i)
                s.record_decision(i, Decision(Verdict.IGNORE, Strength.INFERRED, "r", model_rule=""))
        path = os.path.join(self._tmp.name, "rt.jsonl")

        def labelled_file(card_cap):
            lines = dv.to_jsonl(dv.read_decisions(self.home), blind=True, card_cap=card_cap).splitlines()
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(json.dumps(dict(json.loads(x), label=None)) for x in lines) + "\n")

        labelled_file(1000)
        with self.assertRaises(corpus.CorpusError) as cm:                  # refused like any over-long row
            corpus.load(path, rules, 1000)
        self.assertTrue(any("never read" in p for p in cm.exception.problems), cm.exception.problems)
        got = corpus.load(path, rules, 1000, allow_truncated=True)         # allowed: replayed, not scored
        self.assertEqual([r.truncated_chars for r in got.rows], [0, 500])
        self.assertEqual(got.warnings, ())
        self.assertEqual([r.truncated_chars for r in corpus.load(path, rules, 600, allow_truncated=True).rows],
                         [0, 900], "a smaller cap here cuts more of what the labeller saw")
        warned = corpus.load(path, rules, 2000, allow_truncated=True)      # exported under another cap
        self.assertTrue(any("different" in w for w in warned.warnings), warned.warnings)
        self.assertEqual([r.truncated_chars for r in warned.rows], [0, 0])
        labelled_file(1000)
        with open(path, encoding="utf-8") as f:
            bad = f.read().replace('"truncated_chars": 500', '"truncated_chars": -1')
        with open(path, "w", encoding="utf-8") as f:
            f.write(bad)
        with self.assertRaises(corpus.CorpusError):
            corpus.load(path, rules, 1000, allow_truncated=True)

    def test_the_cap_used_is_written_on_every_line_and_must_be_positive(self):
        """The home does not record the cap a past run used, so the file says which one was assumed;
        a negative cap used to cut from the wrong end."""
        self.populate()
        rows = dv.read_decisions(self.home)
        self.assertEqual({json.loads(x)["card_cap"] for x in dv.to_jsonl(rows, blind=True, card_cap=7).splitlines()}, {7})
        for bad in (0, -5, True, 2.5, "9"):
            with self.subTest(card_cap=bad):
                with self.assertRaises(DecisionsError):
                    dv.to_jsonl(rows, blind=True, card_cap=bad)
        with self.assertRaises(DecisionsError):
            dv.to_jsonl(rows, text_cap=0)

    def test_the_normal_export_carries_the_decision(self):
        self.populate()
        first = json.loads(dv.to_jsonl(dv.read_decisions(self.home)).splitlines()[0])
        self.assertEqual((first["verdict"], first["model_rule"], first["would_accept"]), ("ignore", "x", True))
        self.assertIn("SECRET-REASON", first["reason"])

    def test_unsafe_characters_are_spelled_out_in_both_formats(self):
        """A message must not be able to reorder or hide what a person reads; and the labelling file must
        still parse back to the real text."""
        with Store(self.home) as s:
            s.add_trigger(trig("e1", text=TRICKS, sender="s" + TRICKS, thread="t"))
            s.record_decision(1, Decision(Verdict.IGNORE, Strength.INFERRED, "r" + TRICKS, model_rule="m" + TRICKS))
        rows = dv.read_decisions(self.home)
        table, lines = dv.to_table(rows), dv.to_jsonl(rows)
        for ch in "‮\u009b​⁦﻿ \x00":
            self.assertNotIn(ch, table)
            self.assertNotIn(ch, lines)
        self.assertIn("\\u202e", table)
        self.assertEqual(json.loads(lines.splitlines()[0])["text"], TRICKS[:200])
        blind = json.loads(dv.to_jsonl(rows, blind=True).splitlines()[0])
        self.assertEqual(blind["text"], TRICKS)

    def test_the_table_shows_none_and_no_rule_differently(self):
        self.populate()
        table = dv.to_table(dv.read_decisions(self.home))
        lines = table.splitlines()
        self.assertIn("(none)", lines[2])          # the model said no rule
        self.assertIn(" - ", " " + lines[3] + " ")  # nobody answered
        self.assertIn("made-up", table)


if __name__ == "__main__":
    unittest.main()
