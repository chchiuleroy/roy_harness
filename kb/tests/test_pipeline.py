"""S1c: conversion pipeline (T-W2 rerun, T-W3 reports and visible failures, provenance statements, unit identity across revisions),
chunker (T-K1/K2) and the display rule."""
import json
import os
import random
import unittest

from kb import cas, chunker, colls, conv_registry, db, display, inbox, ids, pipeline
from kb.home import Home

from .support import DbTestCase, tmpdir

H = "a" * 64


class Base(DbTestCase):
    def setUp(self):
        super().setUp()
        self.d = tmpdir(self)
        self.home = Home(os.path.join(self.d, "h"))
        self.home.init()
        self.conn = db.connect(self.home.db_path)
        self.cid = colls.create(self.conn, "docs")
        self.store = cas.BlobStore(self.home.sub("blobs"))
        self.intake = inbox.Intake(self.conn, self.store, self.cid, settle_s=0)
        self.src = os.path.join(self.d, "src")
        os.makedirs(self.src)

    def add(self, name, data, mode="copy"):
        p = os.path.join(self.src, name)
        with open(p, "wb") as f:
            f.write(data)
        return self.intake.add_file(p, mode)

    def one(self, sql, *a):
        return self.conn.execute(sql, a).fetchone()

    def conv(self, revision_id):
        return self.one("SELECT status, converter, version, report_json, params_hash FROM conversions WHERE revision_id=?", revision_id)


class TestPipeline(Base):
    def test_each_type_is_converted_with_a_report_and_ordered_units(self):
        r = {n: self.add(n, d) for n, d in {
            "a.md": b"---\ntitle: Alpha\ntags:\n  - t1\n  - t2\n---\n# H\n\npara one\n\npara two\n",
            "b.html": b"<title>Beta</title><h1>X</h1><p>hello</p>", "c.csv": b"k,v\n1,2\n", "d.txt": "純文字\n\n第二段".encode()}.items()}
        out = pipeline.process_pending(self.conn, self.store)
        self.assertEqual([p.status for p in out], ["ok"] * 4)
        status, converter, version, report, _ = self.conv(r["a.md"].revision_id)
        rep = json.loads(report)
        self.assertEqual((status, converter, version, rep["converter"], rep["status"]), ("ok", "markdown", "1", "markdown", "ok"))
        self.assertIn("units", rep)
        texts = [row[0] for row in self.conn.execute(
            "SELECT json_extract(s.value_json, '$') FROM conversion_units cu JOIN statements s ON s.subject_id = cu.unit_version_id AND s.key='text' "
            "JOIN conversions c ON c.conv_id = cu.conv_id WHERE c.revision_id=? ORDER BY cu.ordinal", (r["a.md"].revision_id,))]
        self.assertEqual(texts, ["# H", "para one", "para two"])

    def test_statements_carry_their_origin_and_evidence(self):
        """W4/P2: every value says who produced it; a title read from the file is native, one made from the file name is derived."""
        a = self.add("a.md", b"---\ntitle: Alpha\ntags:\n  - t1\n---\nbody")
        b = self.add("untitled.md", b"just a paragraph")
        pipeline.process_pending(self.conn, self.store)
        rows = {(sid, k, v): (o, e) for sid, k, v, o, e in self.conn.execute(
            "SELECT subject_id, key, json_extract(value_json,'$'), origin, evidence_json FROM statements WHERE key IN ('title','tag')")}
        self.assertEqual(rows[(a.source_id, "title", "Alpha")][0], "native")
        self.assertEqual(rows[(a.source_id, "tag", "t1")][0], "native")
        self.assertEqual(rows[(b.source_id, "title", "untitled")][0], "derived")
        for _, evidence in rows.values():
            self.assertIn("conv_id", json.loads(evidence))
        self.assertTrue(all(o in ("native", "derived") for o, _ in rows.values()))

    def test_unsupported_and_unavailable_sources_fail_visibly(self):
        """P4/H5: a file kb cannot convert, or whose bytes are gone or changed, is a failed conversion with a reason, not an absence."""
        png = self.add("img.png", b"\x89PNG data")
        ref = self.add("moved.txt", b"original", mode="ref")
        changed = self.add("edited.txt", b"before edit", mode="ref")
        os.remove(os.path.join(self.src, "moved.txt"))
        with open(os.path.join(self.src, "edited.txt"), "wb") as f:
            f.write(b"after edit")
        out = {p.revision_id: p for p in pipeline.process_pending(self.conn, self.store)}
        self.assertEqual((out[png.revision_id].status, out[png.revision_id].reason), ("failed", "unsupported_type"))
        self.assertEqual(out[ref.revision_id].reason, pipeline.SOURCE_UNAVAILABLE)
        self.assertEqual(out[changed.revision_id].reason, pipeline.SOURCE_UNAVAILABLE)   # the changed original is NOT converted as if unchanged
        self.assertEqual(self.one("SELECT count(*) FROM unit_versions")[0], 0)

    def test_partial_conversions_keep_their_warnings(self):
        r = self.add("big5.txt", "繁體中文測試內容".encode("cp950"))
        pipeline.process_pending(self.conn, self.store)
        status, _, _, report, _ = self.conv(r.revision_id)
        self.assertEqual(status, "partial")
        self.assertIn("cp950", json.loads(report)["warnings"][0])

    def test_rerun_converts_nothing_twice(self):
        self.add("a.md", b"# a\n\ntext")
        self.assertEqual(len(pipeline.process_pending(self.conn, self.store)), 1)
        self.assertEqual(pipeline.process_pending(self.conn, self.store), [])
        self.assertEqual(self.one("SELECT count(*) FROM conversions")[0], 1)

    def test_identical_bytes_convert_identically(self):
        """T-W2 through the whole pipeline: two sources with the same bytes give the same unit texts and the same output hash."""
        a = self.add("one.md", b"# T\n\nsame text here")
        b = self.add("two.md", b"# T\n\nsame text here")
        pipeline.process_pending(self.conn, self.store)
        hashes = [r[0] for r in self.conn.execute("SELECT output_sha256 FROM conversions")]
        self.assertEqual(len(set(hashes)), 1)
        self.assertEqual(self.one("SELECT count(DISTINCT fingerprint) FROM unit_versions")[0], 2)

    def test_a_crash_inside_one_conversion_leaves_no_partial_rows_and_the_rest_survive(self):
        self.add("a.md", b"# a\n\nfirst")
        self.add("b.md", b"# b\n\nsecond")
        real = conv_registry.pick
        calls = []

        def flaky(name, head=b""):
            calls.append(name)
            if name == "b.md":
                raise RuntimeError("converter bug")
            return real(name, head)
        conv_registry.pick = flaky
        self.addCleanup(setattr, conv_registry, "pick", real)
        with self.assertRaises(RuntimeError):
            pipeline.process_pending(self.conn, self.store)
        self.assertEqual(self.one("SELECT count(*) FROM conversions")[0], 1)        # a.md is whole, b.md left nothing behind
        conv_registry.pick = real
        self.assertEqual(len(pipeline.process_pending(self.conn, self.store)), 1)   # b.md is picked up on the next run
        self.assertEqual(self.one("SELECT count(*) FROM conversions")[0], 2)


class TestUnitIdentityAcrossRevisions(Base):
    def test_an_edit_gives_a_new_version_of_the_same_unit_and_untouched_units_keep_everything(self):
        """A unit is the 'same paragraph' across edits; only its text (its version) changes. Untouched units are untouched."""
        first = self.add("doc.md", b"# T\n\nalpha\n\nbeta\n\ngamma\n")
        pipeline.process_pending(self.conn, self.store)
        self.add("doc.md", b"# T\n\nalpha\n\nBETA EDITED\n\ngamma\n")
        pipeline.process_pending(self.conn, self.store)

        def versions(rev):
            return {json.loads(loc)["key"]: (u, uv, fp) for u, uv, loc, fp in self.conn.execute(
                "SELECT uv.unit_id, uv.unit_version_id, uv.locator_json, uv.fingerprint FROM conversion_units cu "
                "JOIN unit_versions uv ON uv.unit_version_id=cu.unit_version_id JOIN conversions c ON c.conv_id=cu.conv_id WHERE c.revision_id=?", (rev,))}
        v1 = versions(first.revision_id)
        second_rev = self.one("SELECT revision_id FROM revision_observations ORDER BY obs_id DESC LIMIT 1")[0]
        v2 = versions(second_rev)
        self.assertEqual(set(v1), set(v2))
        edited = [k for k in v1 if v1[k][2] != v2[k][2]]
        self.assertEqual(len(edited), 1)
        for k in v1:
            self.assertEqual(v1[k][0], v2[k][0], "the unit id must not change")
            if k not in edited:
                self.assertEqual(v1[k][1], v2[k][1], "an untouched unit keeps its unit version too")
            else:
                self.assertNotEqual(v1[k][1], v2[k][1])

    def test_going_back_to_the_first_text_reuses_the_first_conversion_result(self):
        a = self.add("d.txt", b"first text")
        pipeline.process_pending(self.conn, self.store)
        self.add("d.txt", b"second text")
        pipeline.process_pending(self.conn, self.store)
        back = self.add("d.txt", b"first text")
        self.assertEqual(back.revision_id, a.revision_id)
        self.assertEqual(pipeline.process_pending(self.conn, self.store), [])    # that revision already has a conversion


class TestChunker(unittest.TestCase):
    def units(self, spec):
        return [(f"uv_{chr(97 + i) * 20}", text, path) for i, (text, path) in enumerate(spec)]

    def test_small_units_merge_up_to_the_limit_and_never_cross_sections(self):
        u = self.units([("a" * 300, ("S1",)), ("b" * 300, ("S1",)), ("c" * 300, ("S1",)), ("d" * 100, ("S2",))])
        ch = chunker.chunk_units(u, {"limit": 700, "hard": 2400})
        self.assertEqual([c.text.count("\n\n") + 1 for c in ch], [2, 1, 1])        # a+b fit (602), c would make 904, S2 is a new section
        self.assertEqual([len(c.unit_version_ids) for c in ch], [2, 1, 1])
        self.assertTrue(all(len(c.text) <= 700 for c in ch))

    def test_an_oversize_unit_is_cut_at_sentence_boundaries_without_losing_text(self):
        sentence = "這是一個很長的句子，用來測試切塊。"
        text = sentence * 200
        ch = chunker.chunk_units(self.units([(text, ("S",))]), {"limit": 800, "hard": 500})
        self.assertGreater(len(ch), 3)
        self.assertEqual("".join(c.text for c in ch), text)
        self.assertTrue(all(len(c.text) <= 500 for c in ch))
        self.assertTrue(all(c.text.endswith("。") for c in ch))                      # cut after a sentence end, not mid-sentence

    def test_a_unit_between_limit_and_hard_stays_whole(self):
        ch = chunker.chunk_units(self.units([("x" * 1500, ("S",))]), {"limit": 800, "hard": 2400})
        self.assertEqual([len(c.text) for c in ch], [1500])

    def test_empty_input_and_blank_units(self):
        self.assertEqual(chunker.chunk_units([]), [])
        self.assertEqual(chunker.chunk_units(self.units([("  \n ", ("S",)), ("", ("S",))])), [])

    def test_ids_are_deterministic_and_depend_on_parameters_and_members(self):
        u = self.units([("hello world", ("S",))])
        a, b = chunker.chunk_units(u), chunker.chunk_units(u)
        self.assertEqual([c.chunk_id for c in a], [c.chunk_id for c in b])
        other_params = chunker.chunk_units(u, {"limit": 900, "hard": 2400})
        self.assertNotEqual(a[0].chunk_id, other_params[0].chunk_id)              # same text, other parameters: a different chunk
        other_unit = chunker.chunk_units([("uv_" + "z" * 20, "hello world", ("S",))])
        self.assertNotEqual(a[0].chunk_id, other_unit[0].chunk_id)
        self.assertEqual(chunker.params_hash(), chunker.params_hash(dict(chunker.DEFAULT_PARAMS)))

    def test_every_character_lands_in_exactly_one_chunk(self):
        """Property check on random documents: no text is lost or duplicated, sizes respect the bounds."""
        rng = random.Random(20261116)
        words = ["alpha", "beta", "測試", "資料。", "end.", "line\n", "x" * 40]
        for trial in range(60):
            spec = [("".join(rng.choice(words) + " " for _ in range(rng.randint(1, 120))), (f"S{rng.randint(0, 3)}",)) for _ in range(rng.randint(1, 8))]
            params = {"limit": rng.choice([100, 300, 800]), "hard": rng.choice([400, 900])}
            ch = chunker.chunk_units(self.units(spec), params)
            joined = "\n\n".join(c.text for c in ch)
            for text, _ in spec:
                if len(text) <= params["hard"] and text.strip():
                    self.assertIn(text, joined)
            self.assertEqual(sum(len(c.text) for c in ch) > 0, any(t.strip() for t, _ in spec))
            for c in ch:
                self.assertTrue(c.text.strip())
                if len(c.unit_version_ids) > 1:
                    self.assertLessEqual(len(c.text), params["limit"])


class TestDisplayRule(unittest.TestCase):
    def s(self, sid, origin, state="committed", supersedes=None):
        return {"statement_id": sid, "origin": origin, "state": state, "supersedes": supersedes}

    def test_priority_human_ai_native_derived(self):
        stm = [self.s("d", "derived"), self.s("n", "native"), self.s("a", "ai"), self.s("h", "human")]
        self.assertEqual(display.choose(stm, approved={"a", "h"})[0], "h")
        self.assertEqual(display.choose(stm, approved={"a"})[0], "a")                 # an unapproved human statement is not displayed
        self.assertEqual(display.choose(stm)[0], "n")
        self.assertEqual(display.choose([self.s("d", "derived")])[0], "d")

    def test_human_and_ai_exist_for_display_only_with_an_approval(self):
        self.assertIsNone(display.choose([self.s("h", "human")])[0])
        self.assertIsNone(display.choose([self.s("a", "ai")])[0])

    def test_a_superseded_statement_is_history(self):
        stm = [self.s("old", "native"), self.s("new", "native", supersedes="old")]
        shown, alternates = display.choose(stm)
        self.assertEqual((shown, alternates), ("new", ["old"]))

    def test_ai_cannot_replace_a_human_statement_without_the_override(self):
        """Gate 2 display rule: protected human content stays unless the user saw the difference and agreed (override_protected)."""
        stm = [self.s("h", "human"), self.s("ai", "ai", supersedes="h")]
        self.assertEqual(display.choose(stm, approved={"h", "ai"})[0], "h")
        self.assertEqual(display.choose(stm, approved={"h", "ai"}, override_ok={"ai"})[0], "ai")

    def test_rejected_never_counts_and_candidates_do(self):
        self.assertIsNone(display.choose([self.s("r", "native", state="rejected")])[0])
        self.assertEqual(display.choose([self.s("c", "native", state="candidate")])[0], "c")

    def test_ties_break_on_the_statement_id(self):
        self.assertEqual(display.choose([self.s("b", "native"), self.s("a", "native")])[0], "a")
        self.assertEqual(display.choose([self.s("a", "native"), self.s("b", "native")])[0], "a")

    def test_empty(self):
        self.assertEqual(display.choose([]), (None, []))


if __name__ == "__main__":
    unittest.main()
