"""S1e: search results and their honesty (T-J2 what a hit shows, T-S1 one visibility rule, T-S2 candidate versus committed, T-S3 missing and
outdated evidence) and the deterministic relations."""
import os
import unittest
from unittest import mock

from kb import cas, colls, commit, pipeline, relations
from kb.errors import ArtifactsMissing
from kb.profile import Profile
from kb.search import Searcher

from .world import World


class Base(World):
    def searcher(self):
        return Searcher(self.home, self.conn)


class TestHits(Base):
    def test_finding_the_position_of_a_hit_never_scans_a_whole_table(self):
        """Found by the S3 midterm latency measurement: on a home of 47,694 chunks the position lookup scanned every row of conversion_units for
        each hit (about 113 ms per hit, 5 s for 40 hits), which no small test shows. What a lookup costs is read from the query plan."""
        self.seed_docs()
        self.build()
        seen = []
        self.conn.set_trace_callback(seen.append)
        try:
            hits = self.searcher().sources("falcon harbor dawn")
        finally:
            self.conn.set_trace_callback(None)
        self.assertTrue(hits)
        lookups = [q for q in seen if "conversion_units" in q and q.lstrip().upper().startswith("SELECT")]
        self.assertTrue(lookups)
        for q in lookups:
            plan = " | ".join(str(r[-1]) for r in self.conn.execute("EXPLAIN QUERY PLAN " + q))
            self.assertNotRegex(plan, r"SCAN (cu|conversion_units)\b", plan)

    def test_a_hit_shows_where_it_is_and_why_it_matched(self):
        """J2: source, place in the source, matched words, a snippet around them: enough to judge the hit without opening the file."""
        self.seed_docs()
        self.build()
        hit = self.searcher().sources("falcon harbor dawn")[0]
        self.assertEqual((hit.state, hit.source_id, hit.status, hit.outdated), ("committed", self.a.source_id, "ok", False))
        self.assertEqual((hit.title, hit.name), ("Alpha", "alpha.md"))
        self.assertTrue(hit.location.endswith("/src/alpha.md"))
        self.assertEqual(hit.why, ["dawn", "falcon", "harbor"])
        self.assertIn("falcon", hit.snippet)
        self.assertEqual(hit.locator["type"], "md")
        self.assertIn("line", hit.locator)
        self.assertEqual(hit.locator["heading"], ["Alpha"])

    def test_only_words_really_in_the_chunk_are_listed_as_the_reason(self):
        self.seed_docs()
        self.build()
        hit = self.searcher().sources("falcon zebra")[0]
        self.assertEqual(hit.why, ["falcon"])

    def test_chinese_queries_match_by_bigrams(self):
        self.seed_docs()
        self.build()
        hits = self.searcher().sources("貝氏推論")
        self.assertEqual(hits[0].source_id, self.b.source_id)
        self.assertEqual(hits[0].why, ["推論", "氏推", "貝氏"])          # all three bigrams of 貝氏推論

    def test_page_level_view_has_one_hit_per_source(self):
        self.add("long.md", "# Long\n\n" + "\n\n".join(f"paragraph {i} about whales and the sea " + "x" * 400 for i in range(10)))
        self.add("short.md", "# Short\n\nwhales appear once here\n")
        self.build()
        sources = [h.source_id for h in self.searcher().sources("whales")]
        self.assertEqual(len(sources), len(set(sources)))
        self.assertEqual(len(sources), 2)
        self.assertGreater(len([h for h in self.searcher().chunks("whales", k=20)]), 2)

    def test_more_occurrences_rank_higher(self):
        self.add("many.txt", "kelp kelp kelp kelp and more kelp")
        self.add("one.txt", "a single mention of kelp in a longer sentence about other things entirely")
        self.build()
        self.assertEqual(self.searcher().sources("kelp")[0].name, "many.txt")

    def test_provenance_checks_are_done_only_for_returned_hits_and_the_manifest_is_read_once(self):
        """Why: the status check hashes the original file and the manifest lists every chunk. Doing either for every candidate (or every
        query) made a first version take 600 ms per query on the wiki; this pins the cheap shape."""
        for i in range(12):
            self.add(f"doc{i}.txt", f"every document mentions plankton number {i} " + "filler " * 50)
        self.build()
        s = self.searcher()
        status_calls, manifest_reads = [], []
        real_status, real_active = s._status, commit.active_artifacts
        s._status = lambda *a: (status_calls.append(a), real_status(*a))[1]
        commit.active_artifacts = lambda *a: (manifest_reads.append(a), real_active(*a))[1]
        self.addCleanup(setattr, commit, "active_artifacts", real_active)
        self.assertEqual(len(s.sources("plankton", k=3)), 3)
        self.assertEqual(len(status_calls), 3)                                  # not one per candidate chunk
        s.sources("plankton", k=3)
        s.chunks("plankton", k=2)
        self.assertEqual(len(manifest_reads), 1)

    def test_empty_and_wordless_queries_return_nothing(self):
        self.seed_docs()
        self.build()
        for q in ("", "   ", "!!! ???", "a"):
            self.assertEqual(self.searcher().chunks(q), [])

    def test_identical_chunks_from_different_sources_are_one_hit_with_the_other_places_listed(self):
        """WC13 on the read side: a copy must not appear twice in the results."""
        self.add("one.txt", "the same unique sentence about glaciers")
        self.add("copy.txt", "the same unique sentence about glaciers")
        self.build()
        hits = self.searcher().chunks("glaciers")
        self.assertEqual(len(hits), 1)
        self.assertEqual(len(hits[0].also_in), 1)


class TestEvidenceStatus(Base):
    def test_a_moved_or_edited_original_is_reported_not_silently_served(self):
        """T-S3 / J2: the index may know text the file no longer has. The hit must say so."""
        a = self.intake.add_file(self.write_file("ref_gone.txt", "evidence about volcanoes"), mode="ref")
        b = self.intake.add_file(self.write_file("ref_edited.txt", "evidence about tsunamis"), mode="ref")
        c = self.intake.add_file(self.write_file("copied.txt", "evidence about glaciers"), mode="copy")
        self.build()
        os.remove(os.path.join(self.src, "ref_gone.txt"))
        with open(os.path.join(self.src, "ref_edited.txt"), "w") as f:
            f.write("different now")
        os.remove(os.path.join(self.src, "copied.txt"))
        s = self.searcher()
        self.assertEqual(s.sources("volcanoes")[0].status, "source_unavailable")
        self.assertEqual(s.sources("tsunamis")[0].status, "source_changed")
        self.assertEqual(s.sources("glaciers")[0].status, "ok")                      # the managed copy still holds the exact bytes
        self.assertEqual((a.outcome, b.outcome, c.outcome), ("new", "new", "new"))

    def write_file(self, name, text):
        p = os.path.join(self.src, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_a_newer_revision_marks_the_hit_outdated_until_the_next_commit(self):
        self.add("doc.txt", "original wording about comets")
        self.build()
        self.assertFalse(self.searcher().sources("comets")[0].outdated)
        self.add("doc.txt", "revised wording about asteroids")
        self.assertTrue(self.searcher().sources("comets")[0].outdated)               # the index still holds the old text and says so
        self.build()
        self.assertEqual(self.searcher().sources("comets"), [])
        self.assertFalse(self.searcher().sources("asteroids")[0].outdated)

    def test_without_a_commit_nothing_is_served_as_evidence(self):
        self.seed_docs()
        with self.assertRaises(ArtifactsMissing):
            self.searcher().chunks("falcon")


class TestCandidates(Base):
    def test_a_new_file_is_found_as_an_unprocessed_candidate_and_never_as_evidence(self):
        """J3 / T-S2: fresh files are findable at once, clearly labelled, and absent from committed results."""
        self.seed_docs()
        self.build()
        new = self.add("fresh.md", "# Fresh\n\nnotes about meteorology and clouds\n")
        s = self.searcher()
        self.assertEqual(s.chunks("meteorology"), [])
        cands = s.candidates("meteorology clouds")
        self.assertEqual([(h.state, h.source_id, h.chunk_id) for h in cands], [("candidate", new.source_id, None)])
        self.assertIn("meteorology", cands[0].why)
        self.build()
        self.assertEqual(s.candidates("meteorology"), [])                              # committed now: no longer a candidate
        self.assertEqual(s.sources("meteorology")[0].state, "committed")

    def test_an_edited_source_is_a_candidate_for_its_new_text_only(self):
        self.add("doc.txt", "old text about lichen")
        self.build()
        self.add("doc.txt", "new text about moss")
        s = self.searcher()
        self.assertEqual([h.state for h in s.candidates("moss")], ["candidate"])
        self.assertEqual(s.candidates("lichen"), [])
        self.assertTrue(s.sources("lichen")[0].outdated)

    def test_candidates_work_before_the_first_commit(self):
        self.seed_docs()
        self.assertEqual({h.source_id for h in self.searcher().candidates("falcon harbor")}, {self.a.source_id})

    def test_a_collection_that_must_not_be_indexed_is_not_searched_as_a_candidate_either(self):
        secret = colls.create(self.conn, "diary", Profile(sensitivity="secret", index_plaintext=False))
        self.add("private.txt", "confidential plans about the harbor", collection=secret)
        self.assertEqual(self.searcher().candidates("confidential"), [])


class TestRelations(Base):
    def setUp(self):
        super().setUp()
        self.page = self.add("Page One.md", "# Page One\n\nsee [[page_two]] and [[Missing Page]] and [[Page One]] and [[Shared]]\n")
        self.two = self.add("page_two.md", "---\ntitle: The Second\ntags:\n  - x\n  - y\n---\nback to [[PAGE ONE]]\n")
        self.add("Shared.md", "# s1")
        self.add("shared.txt", "s2")

    def derive(self):
        pipeline.process_pending(self.conn, self.store)
        return relations.derive(self.conn)

    def edges(self):
        return {(s, __import__("json").loads(v)["ref"]) for s, v in self.conn.execute(
            "SELECT subject_id, value_json FROM statements WHERE key='links_to_source' AND state != 'rejected'")}

    def test_links_resolve_by_name_or_title_and_only_when_unambiguous(self):
        counts = self.derive()
        self.assertEqual(self.edges(), {(self.page.source_id, self.two.source_id), (self.two.source_id, self.page.source_id)})
        self.assertEqual((counts["resolved"], counts["unresolved"], counts["ambiguous"]), (2, 2, 1))   # unresolved: Missing Page and the self link; ambiguous: Shared (two sources)
        for origin, evidence in self.conn.execute("SELECT origin, evidence_json FROM statements WHERE key='links_to_source'"):
            self.assertEqual(origin, "derived")
            self.assertIn("wikilink_name_match@1", evidence)

    def test_a_link_to_itself_is_not_an_edge(self):
        self.derive()
        self.assertNotIn((self.page.source_id, self.page.source_id), self.edges())

    def test_deriving_again_changes_nothing(self):
        self.derive()
        before = self.conn.execute("SELECT count(*) FROM statements").fetchone()[0]
        counts = self.derive()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM statements").fetchone()[0], before)
        self.assertEqual((counts["rejected"], counts["restored"]), (0, 0))

    def test_committed_edges_are_readable_from_both_ends(self):
        self.derive()
        self.build()
        out, inc = relations.neighbors(self.conn, self.page.source_id)
        self.assertEqual((out, inc), ([self.two.source_id], [self.two.source_id]))
        self.assertEqual(relations.neighbors(self.conn, self.two.source_id), ([self.page.source_id], [self.page.source_id]))

    def test_an_edge_that_stops_resolving_is_rejected_and_comes_back_when_it_resolves_again(self):
        """Edited-out links must leave the next commit (history is kept as a rejected row) and return if the link is written again."""
        self.derive()
        self.build()
        self.add("Page One.md", "# Page One\n\nthe link to the other page was removed\n")
        counts = self.derive()
        self.assertEqual(counts["rejected"], 1)
        self.assertEqual(self.edges(), {(self.two.source_id, self.page.source_id)})
        self.assertEqual(self.build().outcome, "created")
        self.assertEqual(relations.neighbors(self.conn, self.page.source_id), ([], [self.two.source_id]))
        self.add("Page One.md", "# Page One\n\nsee [[page_two]] again\n")
        counts = self.derive()
        self.assertEqual((counts["rejected"], counts["restored"]), (0, 1))
        self.build()
        self.assertEqual(relations.neighbors(self.conn, self.page.source_id), ([self.two.source_id], [self.two.source_id]))
        self.assertEqual(self.one("SELECT count(*) FROM statements WHERE key='links_to_source'")[0], 2)      # one row per edge, ever

    def test_multi_valued_tags_are_all_displayed(self):
        """A source with two tags must keep both; the one-value-per-key display rule must not demote the second to an alternate."""
        self.derive()
        r = self.build()
        shown = {v for (v,) in self.conn.execute(
            "SELECT json_extract(s.value_json,'$') FROM statements s JOIN commit_statements cs ON cs.statement_id=s.statement_id "
            "WHERE cs.commit_id=? AND s.key='tag' AND cs.role='display' AND s.subject_id=?", (r.commit_id, self.two.source_id))}
        self.assertEqual(shown, {"x", "y"})

    def test_statements_of_an_older_conversion_do_not_compete_with_the_current_ones(self):
        """After a title edit only the new title may be displayed in the next commit; the old one is history, not an alternate in the commit."""
        self.derive()
        first = self.build()
        self.add("page_two.md", "---\ntitle: Renamed Title\n---\nback to [[PAGE ONE]]\n")
        pipeline.process_pending(self.conn, self.store)
        second = commit.build(self.home, self.conn)
        titles = [v for (v,) in self.conn.execute(
            "SELECT json_extract(s.value_json,'$') FROM statements s JOIN commit_statements cs ON cs.statement_id=s.statement_id "
            "WHERE cs.commit_id=? AND s.key='title' AND s.subject_id=? AND cs.role='display'", (second.commit_id, self.two.source_id))]
        self.assertEqual(titles, ["Renamed Title"])
        old = self.one("SELECT count(*) FROM commit_statements cs JOIN statements s ON s.statement_id=cs.statement_id WHERE cs.commit_id=? "
                       "AND s.key='title' AND s.subject_id=?", second.commit_id, self.two.source_id)[0]
        self.assertEqual(old, 1)
        self.assertIsNotNone(first)


if __name__ == "__main__":
    unittest.main()


class TestCachedOriginalCheck(Base):
    """E1: a hit used to re-read and re-hash its whole original file every time. A long-lived searcher may now trust a file that is unchanged by path,
    size and modification time since ITS OWN last full check. The default check, `kb open` and `kb doctor --deep` still read the file."""

    def setUp(self):
        super().setUp()
        self.seed_docs()
        self.build()
        self.sha = self.one("SELECT sha256 FROM blobs LIMIT 1")[0]
        self.path = self.store.blob_path(self.sha)

    def counting(self):
        from kb import cas
        calls = []
        real = cas.hash_file

        def counted(path):
            calls.append(path)
            return real(path)
        return mock.patch.object(cas, "hash_file", counted), calls

    def test_the_second_cached_check_of_an_unchanged_file_does_not_read_it_again(self):
        patch, calls = self.counting()
        with patch:
            self.assertEqual(self.store.verify(self.conn, self.sha, cached=True), cas.OK)
            self.assertEqual(self.store.verify(self.conn, self.sha, cached=True), cas.OK)
        self.assertEqual(len(calls), 1)

    def test_the_default_check_always_reads_the_file(self):
        patch, calls = self.counting()
        with patch:
            self.store.verify(self.conn, self.sha, cached=True)
            self.store.verify(self.conn, self.sha)
            self.store.verify(self.conn, self.sha)
        self.assertEqual(len(calls), 3)

    def test_a_search_hit_checks_its_original_once_per_searcher_not_once_per_query(self):
        searcher = self.searcher()
        patch, calls = self.counting()
        with patch:
            searcher.sources("falcon harbor dawn")
            first = len(calls)
            searcher.sources("falcon harbor dawn")
            searcher.sources("falcon harbor")
        self.assertGreater(first, 0)
        self.assertEqual(len(calls), first)
        self.assertEqual(self.searcher().sources("falcon harbor dawn")[0].status, "ok")      # a new searcher starts with a full check

    def test_a_file_changed_in_size_or_time_is_seen_again_even_by_the_cached_check(self):
        import os
        self.store.verify(self.conn, self.sha, cached=True)
        os.chmod(self.path, 0o666)
        with open(self.path, "r+b") as f:
            data = f.read()
            f.seek(0)
            f.write(b"X" + data[1:])                                                  # same size, new modification time
        self.assertEqual(self.store.verify(self.conn, self.sha, cached=True), cas.MISMATCH)
        with open(self.path, "ab") as f:
            f.write(b"more")                                                          # a different size
        self.assertEqual(self.store.verify(self.conn, self.sha, cached=True), cas.MISMATCH)

    def test_a_change_of_size_that_keeps_the_time_is_seen_by_the_cached_check(self):
        import os
        self.store.verify(self.conn, self.sha, cached=True)
        stat = os.stat(self.path)
        os.chmod(self.path, 0o666)
        with open(self.path, "ab") as f:
            f.write(b"more")
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))                  # the time is put back, the size is not
        self.assertEqual(self.store.verify(self.conn, self.sha, cached=True), cas.MISMATCH)

    def test_a_file_that_went_missing_and_came_back_different_is_not_taken_for_the_old_one(self):
        import os
        self.store.verify(self.conn, self.sha, cached=True)
        stat = os.stat(self.path)
        with open(self.path, "rb") as f:
            size = len(f.read())
        os.chmod(self.path, 0o666)
        os.remove(self.path)
        self.assertEqual(self.store.verify(self.conn, self.sha, cached=True), cas.MISSING)
        with open(self.path, "wb") as f:
            f.write(b"Y" * size)                                                      # the same size, other content
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))                  # and the old time: only a forgotten stamp would accept it
        self.assertEqual(self.store.verify(self.conn, self.sha, cached=True), cas.MISMATCH)

    def test_a_change_that_keeps_size_and_time_is_the_known_limit_and_the_full_check_still_finds_it(self):
        import os
        self.store.verify(self.conn, self.sha, cached=True)
        stat = os.stat(self.path)
        os.chmod(self.path, 0o666)
        with open(self.path, "r+b") as f:
            data = f.read()
            f.seek(0)
            f.write(b"X" + data[1:])
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))                  # the time is put back
        self.assertEqual(self.store.verify(self.conn, self.sha, cached=True), cas.OK)      # the limit, stated in the docstring
        self.assertEqual(self.store.verify(self.conn, self.sha), cas.MISMATCH)             # the exact check is not fooled
        self.assertEqual(self.store.verify(self.conn, self.sha, cached=True), cas.MISMATCH)   # and it forgot the stamp

    def test_a_file_that_went_missing_is_missing_also_to_the_cached_check(self):
        import os
        self.store.verify(self.conn, self.sha, cached=True)
        os.chmod(self.path, 0o666)
        os.remove(self.path)
        self.assertEqual(self.store.verify(self.conn, self.sha, cached=True), cas.MISSING)
