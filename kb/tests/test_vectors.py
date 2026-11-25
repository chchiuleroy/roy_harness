"""S3b: the vector artifact, its cache, semantic and hybrid search (T-I2 index identity, T-S1 same visibility for both indexes)."""
import json
import os
from unittest import mock

import numpy as np

from kb import commit, ids, profile, search, vecindex
from kb.dense import Dense
from kb.errors import CommitFailed, KbError, VectorUnavailable
from kb.vecindex import NeedEmbedding, VectorCache, VectorExtension

from .support import FakeEmbedder
from .world import World

CONCEPTS = {"car": "vehicle", "automobile": "vehicle", "vehicle": "vehicle", "cat": "feline", "kitten": "feline"}
DOCS = {"cars.md": "# Cars\n\nThe automobile repair manual covers brakes and engines.\n",
        "pets.md": "# Pets\n\nA kitten sleeps all day on the sofa.\n",
        "tax.md": "# Tax\n\nThe filing deadline for the annual return is in April.\n"}


class VecWorld(World):
    def setUp(self):
        super().setUp()
        self.emb = FakeEmbedder(concepts=CONCEPTS)
        self.ext = VectorExtension(self.home, self.emb)

    def fill(self, docs=DOCS):
        return {name: self.add(name, text).source_id for name, text in docs.items()}

    def build_v(self, ext=None, **kw):
        return self.build(extensions=[ext or self.ext], **kw)

    def manifest(self):
        cid = commit.active_commit_id(self.conn)
        return cid, commit.read_manifest(commit.segment_dir(self.home, cid))[0]

    def matrix(self):
        cid, _ = self.manifest()
        return np.load(os.path.join(commit.segment_dir(self.home, cid), vecindex.NAME), allow_pickle=False)

    def searcher(self, emb=None):
        return search.Searcher(self.home, self.conn, dense=Dense(emb or self.emb))


class TestCacheAndArtifact(VecWorld):
    def test_every_row_is_the_vector_of_the_member_chunk_at_that_position(self):
        self.fill()
        self.build_v()
        cid, manifest = self.manifest()
        members = sorted(manifest["members"]["chunks"])
        m = self.matrix()
        self.assertEqual(m.shape, (len(members), 64))
        for row, chunk_id in zip(m, members):
            text = self.chunk_text(chunk_id)
            self.assertTrue(np.array_equal(row, self.emb.embed_documents([text])[0]), chunk_id)

    def test_the_manifest_records_the_identity_and_the_file_hash(self):
        self.fill()
        self.build_v()
        _, manifest = self.manifest()
        ext = manifest["extensions"][vecindex.NAME]
        self.assertEqual(ext["signature"], vecindex.signature_of(self.emb.identity))
        self.assertEqual(ext["meta"]["rows"], len(manifest["members"]["chunks"]))
        self.assertIn(vecindex.NAME, manifest["artifacts"])
        self.assertEqual(commit.verify_artifacts(commit.segment_dir(self.home, commit.active_commit_id(self.conn)))[1], [])

    def test_a_text_is_embedded_once_however_often_it_occurs(self):
        self.add("a.md", "# Same\n\nIdentical paragraph about gardens.\n")
        self.add("b.md", "# Same\n\nIdentical paragraph about gardens.\n")
        self.build_v()
        embedded = [t for call in self.emb.document_calls for t in call]
        self.assertEqual(len(embedded), len(set(embedded)))

    def test_nothing_changed_means_unchanged_and_no_new_embedding(self):
        self.fill()
        self.build_v()
        calls = len(self.emb.document_calls)
        self.assertEqual(self.build_v().outcome, "unchanged")
        self.assertEqual(len(self.emb.document_calls), calls)

    def test_adding_vectors_to_a_lexical_only_commit_makes_a_new_commit(self):
        self.fill()
        self.assertEqual(self.build().outcome, "created")
        self.assertEqual(self.build().outcome, "unchanged")
        self.assertEqual(self.build_v().outcome, "created")
        self.assertIn(vecindex.NAME, self.manifest()[1]["extensions"])

    def test_another_model_or_instruction_is_another_index_so_the_commit_is_rebuilt(self):
        self.fill()
        self.build_v()
        for other in (FakeEmbedder(concepts=CONCEPTS, model="other-model"), FakeEmbedder(concepts=CONCEPTS, instruction="other words")):
            self.assertEqual(self.build_v(VectorExtension(self.home, other)).outcome, "created")
            self.assertEqual(self.manifest()[1]["extensions"][vecindex.NAME]["signature"], vecindex.signature_of(other.identity))

    def test_too_many_missing_vectors_stop_the_commit_before_anything_is_written(self):
        self.fill()
        small = VectorExtension(self.home, self.emb, max_inline=1)
        from kb import pipeline
        pipeline.process_pending(self.conn, self.store)
        with self.assertRaises(NeedEmbedding) as cm:
            commit.build(self.home, self.conn, extensions=[small])
        self.assertIn("run kb embed", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0], 0)       # no commit row was even started
        self.assertEqual(os.listdir(self.home.sub("staging")), [])
        self.assertIsNone(commit.active_commit_id(self.conn))

    def test_embedding_ahead_of_time_lets_the_same_commit_go_through(self):
        self.fill()
        from kb import pipeline
        pipeline.process_pending(self.conn, self.store)
        asm = commit.assemble(self.conn)
        cache = VectorCache(vecindex.cache_path(self.home))
        todo = vecindex.missing(cache, self.emb.identity, asm)
        self.assertEqual(len(todo), len(vecindex.chunk_shas(asm)))
        vecindex.embed_missing(cache, self.emb, self.emb.identity, todo, step=2)
        self.assertEqual(vecindex.missing(cache, self.emb.identity, asm), {})
        cache.close()
        self.assertEqual(commit.build(self.home, self.conn, extensions=[VectorExtension(self.home, self.emb, max_inline=0)]).outcome, "created")

    def test_vectors_of_text_that_left_the_commit_leave_the_cache_too(self):
        srcs = self.fill()
        self.build_v()
        cache = VectorCache(vecindex.cache_path(self.home))
        full = cache.count(self.emb.identity.key())
        cache.close()
        sha_of_hidden = ids.text_sha256(self.chunk_text(self.one("SELECT chunk_id FROM chunks WHERE text LIKE '%kitten%'")[0]))      # before the text is erased
        self.decide("delete", {"target_id": srcs["pets.md"], "level": "b"})
        self.build_v()
        cache = VectorCache(vecindex.cache_path(self.home))
        left = cache.count(self.emb.identity.key())
        has_hidden = cache.get_many(self.emb.identity.key(), [sha_of_hidden])
        cache.close()
        self.assertEqual(left, full - 1)
        self.assertEqual(has_hidden, {})

    def test_a_missing_vector_is_an_error_never_a_zero_row(self):
        self.fill()
        with self.assertRaises(KbError):
            self.build_v(NoPrepare(self.home, self.emb))
        self.assertIsNone(commit.active_commit_id(self.conn))

    def test_a_commit_of_nothing_still_carries_an_empty_vector_index_and_searches_to_nothing(self):
        self.assertEqual(self.build_v().outcome, "created")
        self.assertEqual(self.matrix().shape, (0, 64))
        self.assertEqual(self.searcher().chunks("anything", mode="dense"), [])

    def seeded_bad_vector(self, damage):
        """A cache that holds a wrong vector for one chunk (a damaged cache file): the commit copies it, so only the artifact check can stop it."""
        self.fill()
        from kb import pipeline
        pipeline.process_pending(self.conn, self.store)
        asm = commit.assemble(self.conn)
        cache = VectorCache(vecindex.cache_path(self.home))
        todo = vecindex.missing(cache, self.emb.identity, asm)
        vecindex.embed_missing(cache, self.emb, self.emb.identity, todo)
        sha = sorted(todo)[0]
        good = cache.get_many(self.emb.identity.key(), [sha])[sha].copy()
        cache.put_many(self.emb.identity.key(), {sha: damage(good)})
        cache.close()
        return VectorExtension(self.home, self.emb, max_inline=0)

    def test_a_cache_vector_that_is_not_finite_is_named_as_such_and_stops_the_commit(self):
        ext = self.seeded_bad_vector(lambda v: np.where(np.arange(v.shape[0]) == 0, np.nan, v).astype(np.float32))
        with self.assertRaises(CommitFailed) as cm:
            self.build_v(ext)
        self.assertIn("not finite", str(cm.exception))

    def test_a_cache_vector_that_is_not_unit_length_is_named_as_such_and_stops_the_commit(self):
        ext = self.seeded_bad_vector(lambda v: (v * 2).astype(np.float32))
        with self.assertRaises(CommitFailed) as cm:
            self.build_v(ext)
        self.assertIn("not unit length", str(cm.exception))

    def test_a_build_cut_off_before_the_cache_was_pruned_is_made_good_by_the_next_build_even_when_nothing_changed(self):
        """The cache is pruned after the activation. A process that died in between leaves vectors of text that is no longer in the commit; the
        commit is already correct, so the next build reports 'unchanged' and must still finish the clean-up."""
        names = self.fill()
        self.build_v()
        self.add(sorted(names)[0], "# Rewritten\n\nA paragraph about orchards and nothing from before.\n")
        with mock.patch.object(VectorExtension, "finish"):                            # the process dies right after the activation
            self.assertEqual(self.build_v().outcome, "created")
        wanted = len(vecindex.chunk_shas(commit.assemble(self.conn)))
        cache = VectorCache(vecindex.cache_path(self.home))
        self.assertGreater(cache.count(self.emb.identity.key()), wanted)
        cache.close()
        self.assertEqual(self.build_v().outcome, "unchanged")
        cache = VectorCache(vecindex.cache_path(self.home))
        self.assertEqual(cache.count(self.emb.identity.key()), wanted)
        cache.close()

    def test_the_cache_keeps_only_the_current_identity(self):
        self.fill()
        self.build_v()
        other = FakeEmbedder(concepts=CONCEPTS, model="other-model")
        self.build_v(VectorExtension(self.home, other))
        cache = VectorCache(vecindex.cache_path(self.home))
        self.assertEqual(cache.count(self.emb.identity.key()), 0)
        self.assertGreater(cache.count(other.identity.key()), 0)
        cache.close()


class NoPrepare(VectorExtension):
    def prepare(self, conn, asm):
        pass


class BadWriter(VectorExtension):
    """An extension whose artifact disagrees with the members in one chosen way: the commit must refuse to publish it."""

    def __init__(self, *a, damage, **k):
        super().__init__(*a, **k)
        self.damage = damage

    def write(self, directory, asm):
        meta = super().write(directory, asm)
        path = os.path.join(directory, vecindex.NAME)
        m = np.load(path, allow_pickle=False)
        if self.damage == "extra_row":
            m = np.vstack([m, m[:1]])
        elif self.damage == "missing_row":
            m = m[:-1]
        elif self.damage == "swapped_rows":
            m = m[::-1].copy()
        elif self.damage == "nan":
            m = m.copy()
            m[0, 0] = np.nan
        elif self.damage == "not_unit":
            m = m * 2
        elif self.damage == "float64":
            m = m.astype(np.float64)
        np.save(path, m, allow_pickle=False)
        return meta


class TestVectorGhostCheck(VecWorld):
    def test_every_way_the_artifact_can_disagree_with_the_members_stops_the_commit(self):
        self.fill()
        self.build_v()
        active = commit.active_commit_id(self.conn)
        self.add("new.md", "# New\n\nA brand new paragraph to force a new commit.\n")
        for damage in ("extra_row", "missing_row", "swapped_rows", "nan", "not_unit", "float64"):
            with self.subTest(damage=damage):
                with self.assertRaises(CommitFailed):
                    self.build_v(BadWriter(self.home, self.emb, damage=damage))
                self.assertEqual(commit.active_commit_id(self.conn), active)            # the previous commit still serves
                self.assertEqual(self.searcher().chunks("automobile", mode="hybrid")[0].title, "Cars")

    def test_a_vector_artifact_truncated_after_writing_is_caught_by_the_file_check(self):
        self.fill()
        real = commit._write_artifacts

        def truncating(directory, commit_id, parent, asm, *extra):
            sha = real(directory, commit_id, parent, asm, *extra)
            with open(os.path.join(directory, vecindex.NAME), "r+b") as f:
                f.truncate(200)
            return sha
        commit._write_artifacts = truncating
        self.addCleanup(setattr, commit, "_write_artifacts", real)
        with self.assertRaises(CommitFailed):
            self.build_v()


class TestSemanticSearch(VecWorld):
    def setUp(self):
        super().setUp()
        self.fill()
        self.build_v()

    def titles(self, query, mode, k=5):
        return [h.title for h in self.searcher().chunks(query, k, mode=mode)]

    def test_lexical_search_cannot_find_a_synonym_but_semantic_and_hybrid_can(self):
        self.assertEqual(self.titles("car", "lexical"), [])
        self.assertEqual(self.titles("car", "dense")[0], "Cars")
        self.assertEqual(self.titles("car", "hybrid")[0], "Cars")

    def test_a_semantic_hit_says_why_it_matched_and_still_carries_its_provenance(self):
        hit = self.searcher().chunks("car", 3, mode="hybrid")[0]
        self.assertIn("semantic match", hit.why)
        self.assertEqual(hit.status, "ok")
        self.assertTrue(hit.chunk_id.startswith("ck_"))
        self.assertIn("automobile repair manual", hit.snippet)

    def test_an_exact_word_still_ranks_first_in_a_hybrid_search(self):
        self.assertEqual(self.titles("deadline annual return April", "hybrid")[0], "Tax")

    def test_auto_uses_both_when_the_index_matches(self):
        s = self.searcher()
        self.assertEqual(s.chunks("car", 3, mode="auto")[0].title, "Cars")
        self.assertEqual(s.notices, [])

    def test_a_search_without_a_backend_is_lexical_in_auto_mode_and_refused_when_semantic_is_asked_for(self):
        s = search.Searcher(self.home, self.conn)
        self.assertEqual(s.chunks("deadline", 3, mode="auto")[0].title, "Tax")
        self.assertIn("lexical results only", s.notices[0])
        for mode in ("dense", "hybrid"):
            with self.assertRaises(VectorUnavailable):
                s.chunks("deadline", 3, mode=mode)

    def test_vectors_of_another_model_are_refused_not_mixed_in(self):
        other = FakeEmbedder(concepts=CONCEPTS, model="another-model")
        s = self.searcher(other)
        for mode in ("dense", "hybrid"):
            with self.assertRaises(VectorUnavailable) as cm:
                s.chunks("car", 3, mode=mode)
            self.assertIn("model_id", str(cm.exception))
            self.assertIn("kb embed", str(cm.exception))
        results = s.chunks("deadline", 3, mode="auto")                               # falls back, and says so
        self.assertEqual(results[0].title, "Tax")
        self.assertIn("another identity", s.notices[0])
        self.assertEqual(s.chunks("car", 3, mode="auto"), [])                        # the fallback is lexical, so the synonym is not found

    def test_another_instruction_or_dimension_is_also_another_index(self):
        for other in (FakeEmbedder(concepts=CONCEPTS, instruction="other words"), FakeEmbedder(concepts=CONCEPTS, dim=32)):
            with self.assertRaises(VectorUnavailable):
                self.searcher(other).chunks("car", 3, mode="dense")

    def test_a_commit_without_vectors_is_refused_for_semantic_search_with_a_plain_reason(self):
        self.build(force=True)                                                           # a lexical-only commit
        with self.assertRaises(VectorUnavailable) as cm:
            self.searcher().chunks("car", 3, mode="dense")
        self.assertIn("no vector index", str(cm.exception))

    def test_in_a_hybrid_search_the_query_embedding_and_the_lexical_search_run_side_by_side(self):
        import threading
        lexical_started = threading.Event()
        real = search.ftsindex.search

        def lexical(*a, **kw):
            lexical_started.set()
            return real(*a, **kw)
        for mode in ("hybrid", "auto"):
            with self.subTest(mode=mode):
                lexical_started.clear()
                s = self.searcher()
                seen = []
                real_dense = s._dense_list

                def dense_waits_for_the_lexical_side(*a, **kw):
                    seen.append(lexical_started.wait(timeout=10))    # a sequential order would start the lexical search only after this returns
                    return real_dense(*a, **kw)
                with mock.patch.object(search.ftsindex, "search", lexical), mock.patch.object(s, "_dense_list", dense_waits_for_the_lexical_side):
                    hits = s.chunks("annual return deadline", 3, mode=mode)
                self.assertEqual(seen, [True])
                self.assertTrue(hits)

    def test_the_side_by_side_order_gives_the_same_list_as_the_two_calls_one_after_the_other(self):
        from concurrent.futures import Future

        class OneAfterTheOther:
            """The same interface as the thread pool, but the job runs at once in the caller: the old sequential order."""
            def __init__(self, max_workers=1):
                pass

            def submit(self, fn, *a, **kw):
                f = Future()
                f.set_result(fn(*a, **kw))
                return f

            def shutdown(self, wait=True):
                pass
        for q in ("annual return deadline", "car", "deadline", "automobile repair", "tax", "the", "the day in April brakes"):      # the last two match several documents
            for mode in ("hybrid", "auto"):
                for view, k in (("chunks", 5), ("sources", 3)):
                    with self.subTest(query=q, mode=mode, view=view):
                        side_by_side = getattr(self.searcher(), view)(q, k, mode=mode)
                        with mock.patch.object(search, "ThreadPoolExecutor", OneAfterTheOther):
                            sequential = getattr(self.searcher(), view)(q, k, mode=mode)
                        self.assertEqual(side_by_side, sequential)               # the whole hits: order, scores, sources, why-lists, grouped duplicates
                        self.assertTrue(side_by_side)                              # not two empty lists

    def test_when_the_semantic_side_fails_the_lexical_search_has_finished_before_the_error_leaves(self):
        import threading
        s = self.searcher()
        finished = threading.Event()
        real = search.ftsindex.search

        def slow_lexical(*a, **kw):
            import time
            time.sleep(0.3)
            out = real(*a, **kw)
            finished.set()
            return out
        with mock.patch.object(search.ftsindex, "search", slow_lexical), \
                mock.patch.object(s, "_dense_list", side_effect=VectorUnavailable("made up")):
            with self.assertRaises(VectorUnavailable):
                s.chunks("deadline", 3, mode="hybrid")
            self.assertTrue(finished.is_set())                       # nothing keeps running behind the caller
            s2 = self.searcher()
            with mock.patch.object(s2, "_dense_list", side_effect=VectorUnavailable("made up")):
                self.assertEqual(s2.chunks("deadline", 3, mode="auto")[0].title, "Tax")        # auto: the lexical result is still used
                self.assertIn("lexical results only", s2.notices[0])

    def test_the_page_level_view_works_in_every_mode(self):
        for mode in ("lexical", "dense", "hybrid", "auto"):
            self.assertTrue(self.searcher().sources("annual return deadline", 3, mode=mode))
        self.assertEqual(self.searcher().sources("car", 3, mode="lexical"), [])
        for mode in ("dense", "hybrid", "auto"):
            self.assertEqual(self.searcher().sources("car", 3, mode=mode)[0].title, "Cars")

    def test_a_dense_search_scores_by_similarity_and_a_hybrid_one_by_rank_fusion(self):
        dense = self.searcher().chunks("annual return deadline", 3, mode="dense")
        hybrid = self.searcher().chunks("annual return deadline", 3, mode="hybrid")
        self.assertTrue(all(0.2 < h.score <= 1.0 for h in dense[:1]))
        self.assertTrue(all(h.score < 0.1 for h in hybrid))

    def test_the_ranking_is_the_best_first_and_a_short_list_is_the_real_top(self):
        for i in range(70):
            self.add(f"extra{i}.md", f"# Extra {i}\n\nA different paragraph number {i} about topic{i} and nothing else.\n")
        self.build_v()
        cid, manifest = self.manifest()
        directory = commit.segment_dir(self.home, cid)
        dense = Dense(self.emb)
        full = dense.rank("automobile repair", cid, manifest, directory, 40)
        scores = [s for _, _, s in full]
        self.assertEqual(scores, sorted(scores, reverse=True))
        top2 = dense.rank("automobile repair", cid, manifest, directory, 2)
        self.assertEqual([c for c, _, _ in top2], [c for c, _, _ in full[:2]])
        m = self.matrix()
        members = sorted(manifest["members"]["chunks"])
        best = members[int(np.argmax(m @ self.emb.embed_query("automobile repair")))]
        self.assertEqual(top2[0][0], best)
        self.assertEqual(self.chunk_text(best).split("\n")[0][:2], "# ")                    # and it is a real chunk of this commit

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            self.searcher().chunks("car", 3, mode="semantic")

    def test_a_vector_file_that_no_longer_fits_the_members_is_refused_not_searched(self):
        cid, manifest = self.manifest()
        path = os.path.join(commit.segment_dir(self.home, cid), vecindex.NAME)
        m = np.load(path, allow_pickle=False)
        self.assertEqual(m.shape, (3, 64))
        np.save(path, m.reshape(6, 32), allow_pickle=False)                              # same number of bytes, different shape
        self.assertEqual(os.path.getsize(path), manifest["artifacts"][vecindex.NAME]["size"])
        with self.assertRaises(VectorUnavailable) as cm:
            self.searcher().chunks("car", 3, mode="dense")
        self.assertIn("does not match the commit's members", str(cm.exception))


class TestRrf(World):
    def test_rank_fusion_values(self):
        a = [("c1", "s1", 9.0), ("c2", "s2", 5.0)]
        b = [("c2", "s2", 0.9), ("c3", "s3", 0.5)]
        out = search.rrf([a, b], k=60)
        self.assertEqual([c for c, _, _ in out], ["c2", "c1", "c3"])
        self.assertAlmostEqual(out[0][2], 1 / 62 + 1 / 61)
        self.assertAlmostEqual(out[1][2], 1 / 61)
        self.assertAlmostEqual(out[2][2], 1 / 62)

    def test_the_default_constant_is_the_one_the_plan_names(self):
        self.assertEqual(search.RRF_K, 60)
        self.assertAlmostEqual(search.rrf([[("a", "s", 1.0)]])[0][2], 1 / 61)

    def test_an_item_in_one_list_only_is_kept_and_ties_break_by_id(self):
        out = search.rrf([[("b", "s", 1.0)], [("a", "s", 1.0)]])
        self.assertEqual([c for c, _, _ in out], ["a", "b"])


class TestSameVisibilityForBothIndexes(VecWorld):
    """T-S1: lexical and vector search see the same world. Hiding something removes it from both, in every mode."""

    def test_a_source_hidden_at_level_b_is_in_neither_index_nor_in_any_result(self):
        srcs = self.fill()
        self.build_v()
        self.decide("delete", {"target_id": srcs["pets.md"], "level": "b"})
        self.build_v()
        _, manifest = self.manifest()
        in_fts = {r[1] for r in __import__("kb.ftsindex", fromlist=["x"]).read_map(self.active_fts())}
        self.assertEqual(in_fts, set(manifest["members"]["chunks"]))                  # lexical index == members
        self.assertEqual(self.matrix().shape[0], len(in_fts))                         # vector rows == members
        s = self.searcher()
        for query in ("kitten", "cat sofa sleeps", "feline", "all day on the sofa"):
            for mode in ("lexical", "dense", "hybrid"):
                for hit in s.chunks(query, 10, mode=mode):
                    self.assertNotIn("kitten", self.chunk_text(hit.chunk_id), (query, mode))
                    self.assertNotEqual(hit.source_id, srcs["pets.md"], (query, mode))

    def test_a_collection_that_must_not_be_indexed_is_in_neither_index(self):
        from kb import colls
        secret = colls.create(self.conn, "private", profile.Profile(index_plaintext=False))
        self.add("diary.md", "# Diary\n\nThe kitten secret is the combination of the safe.\n", collection=secret)
        self.fill()
        self.build_v()
        for mode in ("lexical", "dense", "hybrid"):
            self.assertEqual([h.title for h in self.searcher().chunks("kitten secret combination safe", 5, mode=mode) if h.title == "Diary"], [])
        self.assertNotIn("combination", " ".join(self.chunk_text(c) for c in self.manifest()[1]["members"]["chunks"]))
