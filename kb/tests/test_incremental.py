"""S2e: incremental work and converter upgrades (T-INC1). A change in one source must cost work for that source only (asserted by counting what was
recomputed), and a new converter version must prove itself on a sample before it replaces anything."""
import os
import unittest
from unittest import mock

from kb import commit, conv_base as cb, upgrade
from kb.conv_text import TextConverter

from .world import World


def converter(version, change=lambda text: text):
    """A text converter of another version whose output is the old one passed through `change`."""
    class C(TextConverter):
        pass
    C.version = version

    def convert(self, data, hints=None):
        res = TextConverter.convert(self, data, hints)
        res.units = [cb.Unit(u.kind, change(u.text), u.locator, u.heading_path) for u in res.units]
        return res
    C.convert = convert
    return C()


def restructured(version, change_units):
    """A text converter of another version whose units are rearranged by `change_units` (reordered, merged, duplicated or dropped)."""
    class C(TextConverter):
        pass
    C.version = version

    def convert(self, data, hints=None):
        res = TextConverter.convert(self, data, hints)
        res.units = change_units(list(res.units))
        return res
    C.convert = convert
    return C()


class IncrementalWorld(World):
    def fill(self, n=8, ext="md"):
        return [self.add(f"doc{i}.{ext}", f"# Doc {i}\n\nParagraph one of document {i}.\n\nParagraph two of document {i}.\n") for i in range(n)]


class TestOnlyWhatChangedIsRecomputed(IncrementalWorld):
    def test_the_first_build_chunks_everything_and_a_repeat_changes_nothing(self):
        self.fill()
        r = self.build()
        self.assertEqual((r.chunked, r.reused), (8, 0))
        again = self.build()
        self.assertEqual((again.outcome, again.reused), ("unchanged", 8))

    def test_one_edited_source_costs_one_conversion_and_one_chunking(self):
        docs = self.fill()
        self.build()
        self.add("doc3.md", "# Doc 3\n\nParagraph one of document 3, edited.\n\nParagraph two of document 3.\n")
        self.assertEqual(len(self.process()), 1)                                    # one conversion, not eight
        r = commit.build(self.home, self.conn)
        self.assertEqual((r.outcome, r.chunked, r.reused), ("created", 1, 7))
        self.assertIn(docs[3].source_id, self.hit_sources("edited"))

    def test_a_correction_to_one_source_re_chunks_only_that_source(self):
        docs = self.fill()
        self.build()
        uv = self.uvs(docs[5].source_id)[1]
        self.decide("patch", {"unit_version_id": uv, "op": "replace", "find": "one of document 5", "replace": "ONE of document 5"})
        r = self.build()
        self.assertEqual((r.chunked, r.reused), (1, 7))

    def test_hiding_one_source_re_chunks_nothing(self):
        docs = self.fill()
        self.build()
        self.decide("delete", {"target_id": docs[0].source_id, "level": "a"})
        r = self.build()
        self.assertEqual((r.chunked, r.reused), (0, 7))
        self.assertNotIn(docs[0].source_id, self.hit_sources("document"))

    def test_the_memo_never_serves_stale_text(self):
        """The signature includes the displayed text of every unit: an edit must change it, whatever the file name or position."""
        docs = self.fill(3)
        self.build()
        self.add("doc1.md", "# Doc 1\n\nBrand new wording about zebras.\n\nParagraph two of document 1.\n")
        self.build()
        self.assertIn(docs[1].source_id, self.hit_sources("zebras"))
        self.assertNotIn(docs[1].source_id, self.hit_sources("one"))

    def test_a_memo_entry_whose_chunks_are_missing_is_ignored(self):
        self.conn.execute("INSERT INTO chunk_memo(signature, chunk_ids_json) VALUES ('sig', '[\"ck_aaaaaaaaaaaaaaaaaaaaaaaaa\"]')")
        self.assertIsNone(commit._memo_get(self.conn, "sig"))

    def test_the_signature_depends_on_text_heading_and_parameters(self):
        base = [("uv_a", "text", ("H",))]
        sig = commit.chunk_signature(base, {"limit": 800, "hard": 2400})
        self.assertNotEqual(sig, commit.chunk_signature([("uv_a", "text2", ("H",))], {"limit": 800, "hard": 2400}))
        self.assertNotEqual(sig, commit.chunk_signature([("uv_a", "text", ("G",))], {"limit": 800, "hard": 2400}))
        self.assertNotEqual(sig, commit.chunk_signature(base, {"limit": 400, "hard": 2400}))
        self.assertEqual(sig, commit.chunk_signature([("uv_a", "text", ["H"])], {"limit": 800, "hard": 2400}))


class TestAHiddenUnitStaysHiddenWhenTheFileIsRewritten(IncrementalWorld):
    """Seventh review: a unit keeps its id only while its structural key is unchanged. A paragraph inserted before a hidden one moved the hidden one to
    another key, it got a new id that no tombstone names, and its text was visible again in the next commit."""
    HIDDEN = "Paragraph one of document 1."
    LEVEL = "a"

    def setUp(self):
        super().setUp()
        self.docs = self.fill(3)
        self.build()
        self.source = self.docs[1].source_id
        unit = self.one("SELECT unit_id FROM unit_versions WHERE unit_version_id = ?", self.uvs(self.source)[1])[0]
        self.decide("delete", {"target_id": unit, "level": self.LEVEL})
        self.build()

    def rewrite(self, *paragraphs):
        self.add("doc1.md", "# Doc 1\n\n" + "\n\n".join(paragraphs) + "\n")
        self.build()

    def present(self, text):
        return (self.one("SELECT count(*) FROM chunks WHERE instr(text, ?) > 0", text)[0]
                + self.one("SELECT count(*) FROM statements WHERE instr(value_json, ?) > 0", text)[0])

    def test_the_text_is_hidden_before_the_rewrite(self):
        self.assertEqual(self.present(self.HIDDEN), 0)
        self.assertGreater(self.present("Paragraph two of document 1."), 0)

    def test_a_paragraph_inserted_before_the_hidden_one_does_not_bring_it_back(self):
        self.rewrite("A new opening paragraph.", self.HIDDEN, "Paragraph two of document 1.")
        self.assertEqual(self.present(self.HIDDEN), 0)
        self.assertNotIn(self.source, self.hit_sources("one"))
        self.assertIn(self.source, self.hit_sources("opening"))                      # the new paragraph is not swallowed by the hidden id
        self.assertGreater(self.present("A new opening paragraph."), 0)

    def test_a_hidden_paragraph_that_moved_down_stays_hidden(self):
        self.rewrite("Paragraph two of document 1.", "Something else entirely.", self.HIDDEN)
        self.assertEqual(self.present(self.HIDDEN), 0)
        self.assertGreater(self.present("Something else entirely."), 0)

    def test_every_unit_of_a_conversion_has_its_own_id(self):
        self.rewrite("A new opening paragraph.", self.HIDDEN, "Paragraph two of document 1.")
        rows = self.one("SELECT count(*), count(DISTINCT uv.unit_id) FROM conversion_units cu JOIN unit_versions uv USING (unit_version_id) "
                        "WHERE cu.conv_id = ?", commit.current_conversion(self.conn, self.source)[0])
        self.assertEqual(rows[0], rows[1])

    def test_a_hidden_paragraph_edited_in_place_stays_hidden(self):
        """Identity by position, as before: the hide follows the paragraph through an edit."""
        self.rewrite(self.HIDDEN + " (edited)", "Paragraph two of document 1.")
        self.assertEqual(self.present("(edited)"), 0)

    def test_a_paragraph_that_was_edited_and_moved_at_once_is_not_recognised(self):
        """KNOWN LIMIT, not a promise: identity is the position or the exact text. A hidden paragraph that is both changed and moved gets a new id, and
        its changed text is visible. If this ever fails because the text is hidden, the limit has been lifted: change the test."""
        self.rewrite("A new opening paragraph.", self.HIDDEN + " (edited)", "Paragraph two of document 1.")
        self.assertGreater(self.present("(edited)"), 0)

    def test_two_copies_of_hidden_text_get_one_hidden_id_between_them(self):
        """Roy hid one paragraph: of two identical ones after the rewrite, one is that paragraph, the other is a copy as visible as it was before the hide."""
        self.rewrite(self.HIDDEN, self.HIDDEN, "Paragraph two of document 1.")
        self.assertEqual(self.one("SELECT count(*) FROM statements WHERE instr(value_json, ?) > 0", self.HIDDEN)[0], 1)
        rows = self.one("SELECT count(*), count(DISTINCT uv.unit_id) FROM conversion_units cu JOIN unit_versions uv USING (unit_version_id) "
                        "WHERE cu.conv_id = ?", commit.current_conversion(self.conn, self.source)[0])
        self.assertEqual(rows[0], rows[1])

    def test_two_texts_of_one_hidden_unit_do_not_give_two_units_its_id(self):
        """Eighth review: a hidden unit has a fingerprint for every text it has had; two new units matching two of them took the same id."""
        self.rewrite(self.HIDDEN + " (edited)", "Paragraph two of document 1.")          # the hidden unit now has two texts
        self.rewrite(self.HIDDEN, self.HIDDEN + " (edited)", "Paragraph two of document 1.")
        rows = self.one("SELECT count(*), count(DISTINCT uv.unit_id) FROM conversion_units cu JOIN unit_versions uv USING (unit_version_id) "
                        "WHERE cu.conv_id = ?", commit.current_conversion(self.conn, self.source)[0])
        self.assertEqual(rows[0], rows[1])

    def test_another_source_with_the_same_words_is_not_hidden(self):
        other = self.add("other.md", "# Other\n\n" + self.HIDDEN + "\n")
        self.build()
        self.assertIn(other.source_id, self.hit_sources("one"))
        self.assertEqual(self.one("SELECT count(*) FROM chunks WHERE instr(text, ?) > 0", self.HIDDEN)[0], 1)


class TestAHiddenUnitStaysHiddenAtLevelBToo(TestAHiddenUnitStaysHiddenWhenTheFileIsRewritten):
    LEVEL = "b"


class TestConverterUpgrade(IncrementalWorld):
    def setUp(self):
        super().setUp()
        self.docs = self.fill(12, "txt")
        self.build()
        self.convs = lambda: self.one("SELECT count(*) FROM conversions")[0]

    def test_a_dual_run_writes_nothing(self):
        before = self.table_hash("conversions", "statements", "unit_versions", "conversion_units")
        report = upgrade.dual_run(self.conn, self.store, converter("2", str.upper), sample=5)
        self.assertEqual(report["sampled"], 5)
        self.assertEqual(self.table_hash("conversions", "statements", "unit_versions", "conversion_units"), before)

    def test_an_upgrade_that_changes_nothing_goes_through_and_costs_no_re_chunking(self):
        n = self.convs()
        uvs_before = self.one("SELECT count(*) FROM unit_versions")[0]
        report, allowed, why, done = upgrade.upgrade(self.conn, self.store, converter("2"))
        self.assertTrue(allowed, why)
        self.assertEqual((report["mean_distance"], report["identical"], done), (0.0, 12, 12))
        self.assertEqual(self.convs(), n + 12)                                        # every source was converted again with the new version
        self.assertEqual(self.one("SELECT count(*) FROM unit_versions")[0], uvs_before)      # identical text keeps its unit versions
        self.assertEqual(commit.current_conversion(self.conn, self.docs[0].source_id)[2], "2")
        r = self.build()
        self.assertEqual((r.chunked, r.reused), (0, 12))                              # and so no chunk was recomputed

    def test_an_upgrade_that_changes_everything_is_refused_and_nothing_is_converted(self):
        n = self.convs()
        report, allowed, why, done = upgrade.upgrade(self.conn, self.store, converter("3", str.upper))
        self.assertFalse(allowed)
        self.assertIn("one sampled source changes by 100%", why)
        self.assertEqual((done, self.convs()), (0, n))
        self.assertEqual(commit.current_conversion(self.conn, self.docs[0].source_id)[2], "1")

    @staticmethod
    def without_the_text_limit():
        """These two tests are about the mean-distance rule; in a document of three short units one changed unit is nearly half of its text, which the
        text limit (tested in TestTheTextLossLimit) would refuse first."""
        real = upgrade.switch_allowed
        return mock.patch.object(upgrade, "switch_allowed", lambda report, sample=upgrade.SAMPLE: real(report, sample, max_loss=1.0))

    def test_the_mean_decides_when_no_single_source_is_too_different(self):
        """A source of three units with one unit changed is at distance 0.5 (4 distinct units, 2 shared). One such source among twelve gives a mean
        of 0.042, inside the limit; two give 0.083, outside it. The single-source limit (0.5) is inclusive."""
        def revise(*numbers):
            targets = {f"Paragraph one of document {n}." for n in numbers}
            return lambda text: text + " (revised)" if text in targets else text
        with self.without_the_text_limit():
            report, allowed, why, _ = upgrade.upgrade(self.conn, self.store, converter("4", revise(4)), sample=12)
        self.assertEqual(report["max_distance"], 0.5)
        self.assertAlmostEqual(report["mean_distance"], 0.5 / 12)
        self.assertTrue(allowed, why)
        self.assertEqual(commit.current_conversion(self.conn, self.docs[4].source_id)[2], "4")

    def test_two_such_sources_are_too_many(self):
        def revise(*numbers):
            targets = {f"Paragraph one of document {n}." for n in numbers}
            return lambda text: text + " (revised)" if text in targets else text
        report2 = upgrade.dual_run(self.conn, self.store, converter("5", revise(4, 5)), sample=12)
        self.assertAlmostEqual(report2["mean_distance"], 1.0 / 12)
        allowed2, why2 = upgrade.switch_allowed(report2, 12, max_loss=1.0)
        self.assertFalse(allowed2)
        self.assertIn("on average", why2)

    def test_a_converter_that_only_reorders_the_units_is_not_identical(self):
        """Found in the S2 review: with sets of units, a document whose paragraphs came out in another order looked unchanged."""
        report = upgrade.dual_run(self.conn, self.store, restructured("6", lambda units: units[::-1]), sample=12)
        self.assertGreater(report["mean_distance"], 0.5)
        self.assertFalse(upgrade.switch_allowed(report, 12)[0])

    def test_distance_sees_order_and_repeats_not_only_the_set_of_units(self):
        d = upgrade.distance
        self.assertEqual(d(["a", "b", "c"], ["a", "b", "c"]), 0.0)
        self.assertEqual(d([], []), 0.0)
        self.assertEqual(d(["a", "b"], ["b", "a"]), 0.5)                       # same set, other order
        self.assertAlmostEqual(d(["a", "a", "b"], ["a", "b"]), 0.2)             # same set, one repeat lost
        self.assertEqual(d(["a", "b", "c"], ["a", "b", "x"]), 0.5)              # content: 4 distinct units, 2 shared (the larger measure wins)
        self.assertEqual(d(["a"], ["b"]), 1.0)

    def test_an_unreadable_original_makes_the_sample_too_small_to_trust(self):
        src = self.docs[0].source_id
        sha = self.one("SELECT blob_sha256 FROM revisions WHERE source_id=?", src)[0]
        path = self.store.blob_path(sha)
        os.chmod(path, 0o666)
        os.remove(path)
        report = upgrade.dual_run(self.conn, self.store, converter("2"), sample=12)
        self.assertEqual((report["unreadable"], report["sampled"]), (1, 11))
        allowed, why = upgrade.switch_allowed(report, 12)
        self.assertFalse(allowed)
        self.assertIn("sample is smaller", why)

    def test_nothing_to_compare_is_not_a_pass(self):
        report = upgrade.dual_run(self.conn, self.store, converter("2"), sample=0)
        self.assertEqual(report["sampled"], 0)
        self.assertEqual(upgrade.switch_allowed(report, 0), (False, "no source could be compared"))

    def test_the_sample_is_deterministic(self):
        a = upgrade.dual_run(self.conn, self.store, converter("2"), sample=4, seed="s")
        b = upgrade.dual_run(self.conn, self.store, converter("2"), sample=4, seed="s")
        c = upgrade.dual_run(self.conn, self.store, converter("2"), sample=4, seed="other")
        self.assertEqual([r["source_id"] for r in a["rows"]], [r["source_id"] for r in b["rows"]])
        self.assertNotEqual([r["source_id"] for r in a["rows"]], [r["source_id"] for r in c["rows"]])

    def test_a_hidden_source_is_not_converted_again_and_its_text_does_not_come_back(self):
        """Second review: the original file of a hidden source stays, so an upgrade that converted it again would write its text back into the database."""
        self.decide("delete", {"target_id": self.docs[3].source_id, "level": "a"})
        self.build()
        self.assertEqual(self.one("SELECT count(*) FROM statements WHERE instr(value_json, 'of document 3.') > 0")[0], 0)
        n = self.convs()
        report, allowed, why, done = upgrade.upgrade(self.conn, self.store, converter("2"))
        self.assertTrue(allowed, why)
        self.assertEqual((report["candidates"], done), (11, 11))
        self.assertEqual(self.convs(), n + 11)
        self.assertEqual(self.one("SELECT count(*) FROM statements WHERE instr(value_json, 'of document 3.') > 0")[0], 0)
        self.assertEqual(commit.current_conversion(self.conn, self.docs[3].source_id)[2], "1")

    def test_a_source_that_holds_a_hidden_unit_is_not_converted_again_by_an_upgrade(self):
        """Third review, round 4: converting the whole source again would write the hidden unit's text anew. Fifth review: erasing it afterwards left a
        window in which it was committed and readable. Sixth review: erasing it at all is not reliable, because a unit keeps its id only while its
        structural key is the same, and a converter that cuts differently would write the text under an id that no tombstone names. So such a source is
        left on its old converter, like a hidden source; nothing is written for it, at no point of the run."""
        from kb import db
        source = self.docs[3].source_id
        uv = self.uvs(source)[1]
        unit = self.one("SELECT unit_id FROM unit_versions WHERE unit_version_id = ?", uv)[0]
        self.decide("delete", {"target_id": unit, "level": "a"})
        self.build()
        self.assertEqual(self.one("SELECT count(*) FROM statements WHERE instr(value_json, 'one of document 3') > 0")[0], 0)
        before = self.convs()
        revise = lambda text: text + " (revised)" if text == "Paragraph one of document 3." else text
        reader = db.connect(self.home.db_path, readonly=True)
        try:
            with self.without_the_text_limit():
                report, allowed, why, done = upgrade.upgrade(self.conn, self.store, converter("4", revise), sample=12)
            self.assertEqual(reader.execute("SELECT count(*) FROM statements WHERE instr(value_json, 'one of document 3') > 0").fetchone()[0], 0)
        finally:
            reader.close()
        self.assertTrue(allowed, why)
        self.assertEqual((report["candidates"], done), (11, 11))                           # twelve sources, one of them holds a hidden unit
        self.assertEqual(report["held_back_hidden_unit"], 1)                               # seventh review: the left-out source is counted, not silent
        self.assertEqual(self.convs(), before + 11)
        self.assertEqual(commit.current_conversion(self.conn, source)[2], "1")
        self.assertEqual(self.one("SELECT count(*) FROM statements WHERE instr(value_json, '(revised)') > 0")[0], 0)

    def test_an_error_in_the_loop_reaches_the_caller_as_it_is(self):
        """Fifth review: a scrub that ran after the loop and failed would have replaced the error that stopped the loop."""
        class Boom(RuntimeError):
            pass
        real, calls = upgrade.pipeline.process_revision, []

        def fail_on_third(conn, store, rev, **kw):
            calls.append(rev)
            if len(calls) == 3:
                raise Boom("the third source")
            return real(conn, store, rev, **kw)
        with mock.patch.object(upgrade.pipeline, "process_revision", fail_on_third):
            with self.assertRaises(Boom):
                upgrade.upgrade(self.conn, self.store, converter("2"))
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(len(calls), 3)

    def test_a_human_correction_survives_an_upgrade_that_keeps_the_text(self):
        uv = self.uvs(self.docs[2].source_id)[1]
        self.decide("patch", {"unit_version_id": uv, "op": "replace", "find": "one of document 2", "replace": "ONE of document 2"})
        upgrade.upgrade(self.conn, self.store, converter("2"))
        self.build()
        self.assertEqual(self.displayed(uv, "text")[1], "human")


if __name__ == "__main__":
    unittest.main()


class TestTheTextLossLimit(IncrementalWorld):
    """Measured on the real wiki (docs/s4_upgrade_thresholds_sample30.json): the distance alone lets a change that DROPS content through when it touches
    few units; the share of text a source loses is the second limit."""

    def long_docs(self, n=6):
        body = "\n\n".join(f"Short paragraph {j}." for j in range(28))
        return [self.add(f"long{i}.txt", f"Intro {i}\n\n{body}\n\n" + ("A very long paragraph about everything. " * 60) + "\n") for i in range(n)]

    def test_a_change_that_drops_the_longest_paragraph_is_close_by_distance_but_refused_for_the_text_it_loses(self):
        self.long_docs()
        self.build()
        drop_long = restructured("7", lambda units: [u for u in units if len(u.text) < 1000])
        report = upgrade.dual_run(self.conn, self.store, drop_long)
        self.assertLessEqual(report["mean_distance"], upgrade.MAX_MEAN_DISTANCE)
        self.assertLessEqual(report["max_distance"], upgrade.MAX_SINGLE_DISTANCE)
        self.assertGreater(report["max_text_lost"], 0.5)
        allowed, why = upgrade.switch_allowed(report)
        self.assertFalse(allowed)
        self.assertIn("loses", why)

    def test_the_limit_is_five_percent_of_the_text_and_exactly_five_passes(self):
        base = {"sampled": 3, "candidates": 3, "mean_distance": 0.0, "max_distance": 0.0}
        self.assertTrue(upgrade.switch_allowed(dict(base, max_text_lost=0.05), 3)[0])
        self.assertFalse(upgrade.switch_allowed(dict(base, max_text_lost=0.06), 3)[0])

    def test_the_measured_numbers_are_the_ones_in_force(self):
        self.assertEqual((upgrade.SAMPLE, upgrade.MAX_MEAN_DISTANCE, upgrade.MAX_SINGLE_DISTANCE, upgrade.MAX_TEXT_LOSS), (30, 0.05, 0.5, 0.05))

    def test_what_is_lost_is_the_text_of_units_that_are_no_longer_there_unchanged(self):
        def pair(text, kind="paragraph", path=()):
            return (upgrade._unit_keys([cb.Unit(kind, text, {}, path)])[0], upgrade._chars(text))
        def unit(text, kind="paragraph", path=()):
            return cb.Unit(kind, text, {}, path)
        old = [pair("a b c d"), pair("ee ff"), pair("ee ff")]                    # 4 + 4 + 4 characters that are not blanks
        same = [unit("a b c d"), unit("ee ff"), unit("ee ff")]
        self.assertEqual(upgrade.text_lost(old, same), 0.0)
        self.assertAlmostEqual(upgrade.text_lost(old, same[:2]), 1 / 3)           # one of the two repeated units is gone: repeats count one for one
        swapped = [unit("zz yy xx ww")] + same[1:]                                # the first unit replaced by other text of the same length
        self.assertAlmostEqual(upgrade.text_lost(old, swapped), 1 / 3)            # a net count of characters would have said 0
        self.assertEqual(upgrade.text_lost(old, same + [unit("something new")]), 0.0)        # additions are not a loss
        self.assertEqual(upgrade.text_lost([], same), 0.0)                        # nothing stored, nothing lost
        self.assertEqual(upgrade.text_lost([pair("x y", "heading", ("h",))], [unit("x y", "heading", ("H",))]), 1.0)     # same words under another heading path

    def test_swapping_the_longest_paragraph_for_other_text_of_the_same_length_is_refused(self):
        """Second review: counting only the net number of characters let a long unit be replaced by an unrelated one of the same length, in a document
        with so many units that the distance stays small."""
        for i in range(6):
            self.add(f"many{i}.txt", f"Intro {i}\n\n" + "\n\n".join(f"Short paragraph {j}." for j in range(80)) + "\n\n" + ("A very long paragraph about everything. " * 60) + "\n")
        self.build()

        def swap(units):
            return [cb.Unit(u.kind, ("Zq " * len(u.text))[:len(u.text)], u.locator, u.heading_path) if len(u.text) >= 1000 else u for u in units]
        report = upgrade.dual_run(self.conn, self.store, restructured("8", swap))
        self.assertLessEqual(report["mean_distance"], upgrade.MAX_MEAN_DISTANCE)       # close by distance: 2 of about 83 units differ
        self.assertLessEqual(report["max_distance"], upgrade.MAX_SINGLE_DISTANCE)
        self.assertGreater(report["max_text_lost"], 0.5)
        allowed, why = upgrade.switch_allowed(report)
        self.assertFalse(allowed)
        self.assertIn("of its text", why)

    def test_a_new_converter_that_fails_loses_everything(self):
        self.long_docs(3)
        self.build()

        class Broken(TextConverter):
            version = "9"

            def convert(self, data, hints=None):
                return cb.ConversionResult(status="failed")
        report = upgrade.dual_run(self.conn, self.store, Broken())
        self.assertEqual(report["max_text_lost"], 1.0)
        self.assertFalse(upgrade.switch_allowed(report)[0])
