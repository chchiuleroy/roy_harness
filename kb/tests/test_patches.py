"""S2c: human corrections as patches (T-P1 anchor still accurate, T-P2 content changed, T-P3 nothing found, T-P4 stale base).
The failure that matters most: a correction applied to the wrong paragraph. A missing correction is visible and fixable; a wrong one looks right."""
import json
import unittest

from kb import commit, patches, review
from kb.errors import ReviewRefused, StaleProposal

from .world import World

DOC = "# Notes\n\nFirst paragraph about falcons.\n\nSecond paragraph has a typpo in it.\n\nThird paragraph about kelp.\n"


class PatchWorld(World):
    def uv_with(self, source_id, needle):
        return next(uv for uv in self.uvs(source_id) if needle in (patches.native_text(self.conn, uv) or ""))

    def patch(self, source_id, needle, **diff):
        return self.decide("patch", dict(unit_version_id=self.uv_with(source_id, needle), **diff))

    def rebuild(self):
        self.process()
        counts = patches.replay(self.conn)
        return counts, commit.build(self.home, self.conn)


class TestACorrectionIsNeverAnEdit(PatchWorld):
    def setUp(self):
        super().setUp()
        self.d1 = self.add("doc.md", DOC)
        self.build()

    def test_the_corrected_text_is_searchable_and_the_original_evidence_is_untouched(self):
        sha_before = self.one("SELECT blob_sha256 FROM revisions WHERE source_id=?", self.d1.source_id)[0]
        out_before = self.one("SELECT output_sha256 FROM conversions WHERE status='ok'")[0]
        self.patch(self.d1.source_id, "typpo", op="replace", find="typpo", replace="typo")
        self.build()
        self.assertIn(self.d1.source_id, self.hit_sources("typo"))
        self.assertNotIn(self.d1.source_id, self.hit_sources("typpo"))
        uv = self.uv_with(self.d1.source_id, "typpo")                      # the converter's own text still says typpo
        self.assertIn("typpo", patches.native_text(self.conn, uv))
        self.assertEqual(self.displayed(uv, "text")[1], "human")
        self.assertEqual(self.one("SELECT blob_sha256 FROM revisions WHERE source_id=?", self.d1.source_id)[0], sha_before)
        self.assertEqual(self.one("SELECT output_sha256 FROM conversions WHERE status='ok'")[0], out_before)
        self.assertTrue(self.store.verify(self.conn, sha_before))

    def test_a_patch_that_changes_nothing_or_cannot_apply_is_refused(self):
        uv = self.uv_with(self.d1.source_id, "typpo")
        for diff in ({"op": "replace", "find": "nowhere", "replace": "x"},
                     {"op": "replace", "find": "a", "replace": "b"},                        # 'a' occurs many times: ambiguous
                     {"op": "set_text", "new": "Second paragraph has a typpo in it."},      # no change
                     {"op": "set_text", "new": "  "},
                     {"op": "explode"}):
            with self.assertRaises(ReviewRefused, msg=str(diff)):
                review.create(self.conn, "patch", dict(unit_version_id=uv, **diff), "roy")
        with self.assertRaises(ReviewRefused):
            review.create(self.conn, "patch", {"unit_version_id": "uv_aaaaaaaaaaaaaaaaaaaaaaaa", "op": "set_text", "new": "x"}, "roy")

    def test_two_corrections_of_one_unit_add_up_in_the_order_they_were_made(self):
        self.patch(self.d1.source_id, "typpo", op="replace", find="typpo", replace="typo")
        self.patch(self.d1.source_id, "typpo", op="replace", find="Second", replace="2nd")
        self.build()
        uv = self.uv_with(self.d1.source_id, "typpo")
        self.assertEqual(self.displayed(uv, "text")[0], "2nd paragraph has a typo in it.")

    def test_a_patch_is_a_proposal_first(self):
        uv = self.uv_with(self.d1.source_id, "typpo")
        before = self.table_hash("patches", "statements")
        pid = review.create(self.conn, "patch", {"unit_version_id": uv, "op": "replace", "find": "typpo", "replace": "typo"}, "roy")
        review.approve(self.conn, pid, "roy")
        self.assertEqual(self.table_hash("patches", "statements"), before)
        self.assertIn("typo", "\n".join(review.describe(self.conn, pid)))

    def test_a_patch_proposed_before_a_newer_commit_is_stale(self):
        """T-P4: the user approves a correction computed on text that has since moved on."""
        uv = self.uv_with(self.d1.source_id, "typpo")
        pid = review.create(self.conn, "patch", {"unit_version_id": uv, "op": "replace", "find": "typpo", "replace": "typo"}, "roy")
        self.add("other.md", "# Other\n\nanother page\n")
        self.build()
        with self.assertRaises(StaleProposal):
            review.approve(self.conn, pid, "roy")
        self.assertEqual(self.one("SELECT count(*) FROM patches")[0], 0)


class TestReplay(PatchWorld):
    def setUp(self):
        super().setUp()
        self.d1 = self.add("doc.md", DOC)
        self.build()
        self.patch(self.d1.source_id, "typpo", op="replace", find="typpo", replace="typo")
        self.build()

    def state(self):
        return self.one("SELECT replay_state FROM patches")[0]

    def test_T_P1_the_correction_follows_its_paragraph_when_paragraphs_before_it_change(self):
        """A paragraph inserted above shifts the structural positions below it; the correction must follow its own paragraph, automatically."""
        self.add("doc.md", DOC.replace("First paragraph about falcons.\n", "A new opening paragraph.\n\nFirst paragraph about falcons.\n"))
        counts, _ = self.rebuild()
        self.assertEqual(counts["moved"], 1)
        self.assertEqual(self.state(), "applied")
        self.assertIn(self.d1.source_id, self.hit_sources("typo"))
        self.assertNotIn(self.d1.source_id, self.hit_sources("typpo"))
        self.assertEqual(self.displayed(self.uv_with(self.d1.source_id, "typpo"), "text")[1], "human")

    def test_several_corrections_of_one_paragraph_all_follow_it(self):
        """Replay applies the corrections in the order they were made, each on the result of the one before (not each on the original)."""
        self.patch(self.d1.source_id, "typpo", op="replace", find="Second", replace="2nd")
        self.build()
        self.add("doc.md", DOC.replace("First paragraph about falcons.\n", "A new opening paragraph.\n\nFirst paragraph about falcons.\n"))
        counts, _ = self.rebuild()
        self.assertEqual(counts["moved"], 1)
        self.assertEqual(self.displayed(self.uv_with(self.d1.source_id, "typpo"), "text")[0], "2nd paragraph has a typo in it.")

    def test_replay_is_idempotent(self):
        self.add("doc.md", DOC.replace("First paragraph about falcons.\n", "A new opening paragraph.\n\nFirst paragraph about falcons.\n"))
        self.rebuild()
        n = self.one("SELECT count(*) FROM statements")[0]
        self.assertEqual(patches.replay(self.conn)["present"], 1)
        self.assertEqual(self.one("SELECT count(*) FROM statements")[0], n)

    def test_T_P2_a_paragraph_that_changed_a_lot_goes_to_a_person_and_is_not_corrected_silently(self):
        self.add("doc.md", DOC.replace("Second paragraph has a typpo in it.", "Entirely different sentences about harbours and tides now."))
        counts, _ = self.rebuild()
        self.assertEqual(counts["needs_review"], 1)
        self.assertEqual(self.state(), "needs_review")
        q = patches.queue(self.conn)
        self.assertEqual((len(q), q[0]["suggest"]["kind"]), (1, "changed"))
        self.assertIn(self.d1.source_id, self.hit_sources("harbours"))                       # the new text is what the index holds
        self.assertEqual(self.displayed(q[0]["suggest"]["uv"], "text")[1], "native")        # no human statement was written there

    def test_resolving_a_conflict_by_dropping_it_orphans_the_patch(self):
        self.add("doc.md", DOC.replace("Second paragraph has a typpo in it.", "Entirely different sentences about harbours and tides now."))
        self.rebuild()
        pid = patches.queue(self.conn)[0]["patch_id"]
        self.assertIsNone(review.resolve_patch(self.conn, pid, False, "roy"))
        self.assertEqual(self.state(), "orphaned")
        self.assertEqual(patches.queue(self.conn), [])

    def test_resolving_a_conflict_by_accepting_needs_a_correction_that_still_fits(self):
        """A find-and-replace whose text is gone cannot be accepted onto the new paragraph: the person must write the new text."""
        self.add("doc.md", DOC.replace("Second paragraph has a typpo in it.", "Entirely different sentences about harbours and tides now."))
        self.rebuild()
        pid = patches.queue(self.conn)[0]["patch_id"]
        with self.assertRaises(ReviewRefused):
            review.resolve_patch(self.conn, pid, True, "roy")
        self.assertEqual(self.state(), "needs_review")                                       # nothing half-done

    def test_accepting_a_conflict_whose_correction_still_fits(self):
        self.add("doc.md", DOC.replace("Second paragraph has a typpo in it.", "Second paragraph still has a typpo, and more words now."))
        self.rebuild()
        q = patches.queue(self.conn)
        self.assertEqual(q[0]["suggest"]["kind"], "changed")
        self.assertGreater(q[0]["suggest"]["score"], 0.6)
        new = review.resolve_patch(self.conn, q[0]["patch_id"], True, "roy")
        self.assertIsNotNone(new)
        self.build()
        self.assertIn(self.d1.source_id, self.hit_sources("typo"))
        self.assertEqual(self.one("SELECT count(*) FROM patches WHERE replay_state='needs_review'")[0], 0)

    def test_a_removed_paragraph_with_a_look_alike_is_offered_to_a_person_not_applied(self):
        """'Third paragraph about kelp.' and 'First paragraph about falcons.' share their shape: replay may suggest it, but never applies it."""
        self.patch(self.d1.source_id, "kelp", op="set_text", new="Third paragraph about giant kelp.")
        self.add("doc.md", DOC.replace("\nThird paragraph about kelp.\n", ""))
        self.rebuild()
        row = self.one("SELECT replay_state, anchor_json FROM patches WHERE op='set_text'")
        self.assertEqual((row[0], json.loads(row[1])["suggest"]["kind"]), ("needs_review", "similar"))
        self.assertEqual(self.one("SELECT count(*) FROM statements WHERE origin='human' AND value_json LIKE '%giant%'")[0], 1)     # only the original correction

    def test_T_P3_a_patch_whose_paragraph_is_gone_is_orphaned_and_applied_nowhere(self):
        doc = DOC.replace("Third paragraph about kelp.", "Quartz crystals form slowly underground.")
        self.add("doc.md", doc)
        self.rebuild()
        self.patch(self.d1.source_id, "Quartz", op="set_text", new="Quartz giant crystals form slowly underground.")
        before = self.one("SELECT count(*) FROM statements WHERE origin='human'")[0]
        self.add("doc.md", doc.replace("\nQuartz crystals form slowly underground.\n", ""))
        self.rebuild()
        self.assertEqual(self.one("SELECT replay_state FROM patches WHERE op='set_text'")[0], "orphaned")
        self.assertEqual(self.one("SELECT count(*) FROM statements WHERE origin='human'")[0], before)       # nothing was written anywhere else
        shown = " ".join(json.loads(r[0]) for r in self.conn.execute(
            "SELECT s.value_json FROM statements s JOIN commit_statements cs ON cs.statement_id=s.statement_id WHERE cs.commit_id=? AND s.key='text'",
            (commit.active_commit_id(self.conn),)))
        self.assertNotIn("giant", shown)

    def test_identical_paragraphs_are_not_guessed_between(self):
        """Two units with the same text: only identical neighbours on both sides identify the patched one; otherwise a person decides."""
        d2 = self.add("dup.md", "Same line.\n\nSame line.\n\nSame line.\n")
        self.build()
        uvs = self.uvs(d2.source_id)
        self.decide("patch", {"unit_version_id": uvs[1], "op": "set_text", "new": "Same line, corrected."})
        self.build()
        self.add("dup.md", "# H\n\nSame line.\n\nSame line.\n\nSame line.\n\nSame line.\n")
        self.rebuild()
        row = self.one("SELECT replay_state, anchor_json FROM patches WHERE op='set_text'")
        self.assertEqual(row[0], "needs_review")
        self.assertEqual(json.loads(row[1])["suggest"]["kind"], "ambiguous")

    def test_the_only_identical_paragraph_elsewhere_is_not_followed_blindly(self):
        """Found in the S2 review: when the patched paragraph is gone and the same words exist once in an unrelated place (boilerplate), the old
        rule moved the correction there. It needs some context in common with the old place; otherwise a person decides."""
        d2 = self.add("two.md", "# One\n\nAlpha content paragraph.\n\nStandard notice paragraph appears here.\n\n# Two\n\nBeta content paragraph.\n")
        self.build()
        self.patch(d2.source_id, "Standard notice", op="set_text", new="Standard NOTICE paragraph appears here.")
        self.build()
        self.add("two.md", "# One\n\nAlpha content, rewritten entirely.\n\n# Two\n\nBeta content paragraph.\n\nStandard notice paragraph appears here.\n")
        self.rebuild()
        row = self.one("SELECT replay_state, anchor_json FROM patches WHERE op='set_text'")
        self.assertEqual((row[0], json.loads(row[1])["suggest"]["kind"]), ("needs_review", "relocated"))
        self.assertEqual(self.displayed(self.uv_with(d2.source_id, "Standard notice"), "text")[1], "native")

    def test_boilerplate_under_the_same_heading_is_not_followed_either(self):
        """Round 2 of the S2 review: same heading, the patched paragraph deleted, one identical sentence left, neighbours all different."""
        d2 = self.add("three.md", "# Terms\n\nIntro paragraph one.\n\nStandard notice paragraph appears here.\n\nClosing paragraph two.\n\n"
                                  "More about other things.\n\nStandard notice paragraph appears here.\n\nLast words.\n")
        self.build()
        uvs = [uv for uv in self.uvs(d2.source_id) if "Standard notice" in patches.native_text(self.conn, uv)]
        self.decide("patch", {"unit_version_id": uvs[0], "op": "set_text", "new": "Standard NOTICE paragraph appears here."})
        self.build()
        # the patched paragraph and its neighbours are rewritten; one identical copy remains, with other neighbours, under the same heading
        self.add("three.md", "# Terms\n\nIntro, rewritten.\n\nClosing, rewritten.\n\nMore about other things.\n\n"
                             "Standard notice paragraph appears here.\n\nLast words.\n")
        self.rebuild()
        row = self.one("SELECT replay_state, anchor_json FROM patches WHERE op='set_text'")
        self.assertEqual((row[0], json.loads(row[1])["suggest"]["kind"]), ("needs_review", "relocated"))

    def test_resolving_is_all_or_nothing(self):
        """Found in the S2 review: if the last step failed, the new correction was already written and the old patch still waited for a decision."""
        self.add("doc.md", DOC.replace("Second paragraph has a typpo in it.", "Second paragraph still has a typpo, and more words now."))
        self.rebuild()
        pid = patches.queue(self.conn)[0]["patch_id"]
        before = self.table_hash("proposals", "approvals", "patches", "statements", "audit_events")
        real = patches.apply_proposal
        patches.apply_proposal = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
        self.addCleanup(setattr, patches, "apply_proposal", real)
        with self.assertRaises(RuntimeError):
            review.resolve_patch(self.conn, pid, True, "roy")
        self.assertEqual(self.table_hash("proposals", "approvals", "patches", "statements", "audit_events"), before)
        self.assertEqual(self.state(), "needs_review")
        self.assertFalse(self.conn.in_transaction)

    def test_a_hidden_source_is_not_replayed(self):
        self.decide("delete", {"target_id": self.d1.source_id, "level": "a"})
        self.add("doc.md", DOC.replace("First paragraph about falcons.\n", "A new opening paragraph.\n\nFirst paragraph about falcons.\n"))
        self.process()
        self.assertEqual(patches.replay(self.conn), {"present": 0, "moved": 0, "needs_review": 0, "orphaned": 0})


class TestResolve(unittest.TestCase):
    """The pure placement function (T-P1..T-P3): every outcome, on hand-made units."""

    def anchor(self, **kw):
        return dict({"uv": "uv_old", "unit_id": "unit_1", "fingerprint": "fp_a", "key": "k", "heading": ["H"], "base_text": "The quick brown fox jumps",
                     "before": "fp_x", "after": "fp_y"}, **kw)

    def unit(self, uv, fp="fp_other", unit_id="unit_9", text="zzz", heading=("H",), before=None, after=None):
        return {"uv": uv, "unit_id": unit_id, "fingerprint": fp, "key": "k", "heading": list(heading), "text": text, "before": before, "after": after}

    def test_each_outcome(self):
        r = patches.resolve
        self.assertEqual(r(self.anchor(), [self.unit("uv_old")]).kind, "present")
        self.assertEqual(r(self.anchor(), [self.unit("uv_new", fp="fp_a", before="fp_x")]).kind, "moved")
        two = [self.unit("uv_1", fp="fp_a", before="fp_x", after="fp_y"), self.unit("uv_2", fp="fp_a", before="fp_q", after="fp_y")]
        res = r(self.anchor(), two)
        self.assertEqual((res.kind, res.uv), ("moved", "uv_1"))
        both = [self.unit("uv_1", fp="fp_a", before="fp_x", after="fp_y"), self.unit("uv_2", fp="fp_a", before="fp_x", after="fp_y")]
        self.assertEqual(r(self.anchor(), both).kind, "ambiguous")
        self.assertEqual(r(self.anchor(), [self.unit("uv_c", unit_id="unit_1", text="nothing alike at all")]).kind, "changed")
        similar = self.unit("uv_s", text="The quick brown fox jumped high")
        self.assertEqual(r(self.anchor(), [similar]).kind, "similar")
        self.assertEqual(r(self.anchor(), [self.unit("uv_o", text="completely unrelated words here")]).kind, "orphaned")
        self.assertEqual(r(self.anchor(), []).kind, "orphaned")

    def test_a_lone_identical_unit_needs_a_shared_neighbour_not_just_a_heading(self):
        """Round 2 of the S2 review: a shared heading alone let a correction land on unrelated boilerplate under the same heading."""
        far = self.unit("uv_far", fp="fp_a", heading=("Elsewhere",), before="fp_q", after="fp_r")
        same_heading = self.unit("uv_h", fp="fp_a", heading=("H",), before="fp_q", after="fp_r")
        for lonely in (far, same_heading):
            self.assertEqual(patches.resolve(self.anchor(), [lonely]).kind, "relocated", lonely["uv"])
        for near in (self.unit("uv_b", fp="fp_a", heading=("Elsewhere",), before="fp_x", after="fp_r"),   # same neighbour before
                     self.unit("uv_a", fp="fp_a", heading=("Elsewhere",), before="fp_q", after="fp_y")):  # same neighbour after
            self.assertEqual(patches.resolve(self.anchor(), [near]).kind, "moved", near["uv"])

    def test_two_document_edges_are_not_a_shared_neighbour(self):
        """Both having 'nothing before' (None) is the start of a document, not evidence that it is the same paragraph."""
        anchor = self.anchor(before=None, after="fp_y")
        edge = self.unit("uv_e", fp="fp_a", before=None, after="fp_other")
        self.assertEqual(patches.resolve(anchor, [edge]).kind, "relocated")
        edge_with_same_after = self.unit("uv_f", fp="fp_a", before=None, after="fp_y")
        self.assertEqual(patches.resolve(anchor, [edge_with_same_after]).kind, "moved")

    def test_a_similar_unit_under_another_heading_is_not_offered(self):
        other = self.unit("uv_s", text="The quick brown fox jumped high", heading=("Elsewhere",))
        self.assertEqual(patches.resolve(self.anchor(), [other]).kind, "orphaned")

    def test_the_present_unit_wins_over_an_identical_copy(self):
        res = patches.resolve(self.anchor(), [self.unit("uv_copy", fp="fp_a"), self.unit("uv_old", fp="fp_a")])
        self.assertEqual((res.kind, res.uv), ("present", "uv_old"))


if __name__ == "__main__":
    unittest.main()
