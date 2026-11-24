"""S2d: AI correction flow (T-AI1). A scripted proposer stands in for the model. What must hold: AI output is only ever a proposal; nothing changes
before approval; after approval only the knowledge layer changes; invented citations cannot be approved; human-written content is protected;
and the approval screen shows what the user is deciding on."""
import json
import os
import unittest

from kb import ai_edit, colls, commit, evidence, review
from kb.errors import PolicyViolation, ReviewRefused
from kb.profile import Profile

from .world import World

SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "support_samples.json")


class AiWorld(World):
    def setUp(self):
        super().setUp()
        self.a = self.add("alpha.md", "# Alpha\n\nThe falcon flies over the harbor at dawn.\n\nKelp forests grow offshore.\n")
        self.build()
        self.uv = next(uv for uv in self.uvs(self.a.source_id) if "falcon" in self.native(uv))

    def native(self, uv):
        from kb import patches
        return patches.native_text(self.conn, uv)

    def proposer(self, value="Falcon Notes", claims=None, extra=None):
        def fn(context):
            self.seen = context
            out = {"statements": [{"subject_id": self.a.source_id, "key": "title", "value": value}], "claims": claims or []}
            out.update(extra or {})
            return out
        return fn

    def good_claim(self):
        return {"text": "The falcon flies over the harbor at dawn.", "citations": [{"unit_version_id": self.uv, "quote": "falcon flies over the harbor"}]}


class TestNothingChangesWithoutApproval(AiWorld):
    def test_a_proposal_leaves_the_knowledge_exactly_as_it_was(self):
        """Gate 1 W7: the AI produces a difference; the knowledge, the commits and the original file are byte for byte the same until approval."""
        sha = self.one("SELECT blob_sha256 FROM revisions")[0]
        before = self.table_hash("statements", "commits", "commit_statements", "tombstones", "patches", "revisions")
        active = commit.active_commit_id(self.conn)
        pid = ai_edit.propose(self.conn, self.proposer(claims=[self.good_claim()]), self.a.source_id, "give it a better title", "roy")
        self.assertEqual(self.table_hash("statements", "commits", "commit_statements", "tombstones", "patches", "revisions"), before)
        self.assertEqual(commit.active_commit_id(self.conn), active)
        self.assertEqual(self.one("SELECT state FROM proposals WHERE proposal_id=?", pid)[0], "pending")
        review.approve(self.conn, pid, "roy")                                       # approved but not applied: still nothing
        self.assertEqual(self.table_hash("statements", "commits", "commit_statements", "tombstones", "patches", "revisions"), before)
        self.assertTrue(self.store.verify(self.conn, sha))

    def test_after_approval_only_the_knowledge_layer_changes(self):
        sha = self.one("SELECT blob_sha256 FROM revisions")[0]
        out_sha = self.one("SELECT output_sha256 FROM conversions")[0]
        pid = ai_edit.propose(self.conn, self.proposer(claims=[self.good_claim()]), self.a.source_id, "retitle", "roy")
        review.approve(self.conn, pid, "roy")
        review.apply(self.conn, pid)
        self.build()
        self.assertEqual(self.displayed(self.a.source_id, "title"), ("Falcon Notes", "ai"))
        self.assertEqual(self.one("SELECT blob_sha256 FROM revisions")[0], sha)
        self.assertEqual(self.one("SELECT output_sha256 FROM conversions")[0], out_sha)
        native = self.one("SELECT value_json FROM statements WHERE subject_id=? AND key='title' AND origin='native'", self.a.source_id)
        self.assertEqual(json.loads(native[0]), "Alpha")                           # the converter's own title is still there, as an alternate

    def test_who_asked_and_who_approved_stay_on_record(self):
        pid = ai_edit.propose(self.conn, self.proposer(), self.a.source_id, "retitle", "小c")
        review.approve(self.conn, pid, "Roy")
        self.assertEqual(self.one("SELECT requested_by FROM proposals WHERE proposal_id=?", pid)[0], "小c")
        self.assertEqual(self.one("SELECT approved_by FROM approvals WHERE proposal_id=?", pid)[0], "Roy")

    def test_the_proposer_cannot_choose_the_origin_of_its_own_statements(self):
        """Whatever the proposer writes, an AI edit is stored as origin 'ai': it can not pose as human-written content."""
        fn = self.proposer(extra={"statements": [{"subject_id": self.a.source_id, "key": "title", "value": "Sneaky", "origin": "human"}]})
        pid = ai_edit.propose(self.conn, fn, self.a.source_id, "retitle", "roy")
        review.approve(self.conn, pid, "roy")
        review.apply(self.conn, pid)
        self.assertEqual(self.one("SELECT origin FROM statements WHERE value_json='\"Sneaky\"'")[0], "ai")

    def test_garbage_from_the_proposer_is_refused_before_it_becomes_a_proposal(self):
        for bad in (lambda c: "not a mapping", lambda c: {"statements": "x"}, lambda c: {"statements": [{"subject_id": "src_zz", "key": "title", "value": "x"}]},
                    lambda c: {"statements": [{"subject_id": self.a.source_id, "key": "title", "value": "x"}] * 51}):
            with self.assertRaises(ReviewRefused):
                ai_edit.propose(self.conn, bad, self.a.source_id, "x", "roy")
        self.assertEqual(self.one("SELECT count(*) FROM proposals")[0], 0)

    def test_an_external_model_only_sees_text_from_a_collection_that_allows_it(self):
        """The profile decides which models may see the text: the proposer must not even be called when it does not allow it."""
        calls = []
        with self.assertRaises(PolicyViolation):
            ai_edit.propose(self.conn, lambda c: calls.append(c) or {"statements": []}, self.a.source_id, "x", "roy", external=True)
        self.assertEqual(calls, [])
        self.conn.execute("UPDATE collections SET profile_json=? WHERE collection_id=?", (Profile(allow_external_model=True).to_json(), self.cid))
        pid = ai_edit.propose(self.conn, self.proposer(), self.a.source_id, "x", "roy", external=True)
        self.assertIsNotNone(pid)


class TestCitations(AiWorld):
    def propose_with(self, claims):
        return ai_edit.propose(self.conn, self.proposer(claims=claims), self.a.source_id, "retitle", "roy")

    def refused(self, pid, **kw):
        with self.assertRaises(ReviewRefused):
            review.approve(self.conn, pid, "roy", **kw)
        self.assertEqual(self.one("SELECT count(*) FROM approvals")[0], 0)
        self.assertEqual(self.one("SELECT state FROM proposals WHERE proposal_id=?", pid)[0], "pending")

    def test_a_citation_to_a_unit_that_does_not_exist_cannot_be_approved(self):
        self.refused(self.propose_with([{"text": "The falcon flies.", "citations": [{"unit_version_id": "uv_aaaaaaaaaaaaaaaaaaaaaaaa", "quote": "falcon"}]}]))

    def test_a_quotation_that_is_not_in_the_cited_unit_cannot_be_approved(self):
        self.refused(self.propose_with([{"text": "The falcon sings.", "citations": [{"unit_version_id": self.uv, "quote": "falcon sings opera"}]}]))

    def test_a_factual_claim_with_no_citation_cannot_be_approved(self):
        self.refused(self.propose_with([{"text": "The falcon flies over the harbor.", "citations": []}]))

    def test_a_citation_without_a_quotation_is_not_a_locator(self):
        self.refused(self.propose_with([{"text": "The falcon flies.", "citations": [{"unit_version_id": self.uv}]}]))

    def test_a_proposal_that_skips_the_checks_is_still_checked_at_approval(self):
        """The stored 'checks' field is evidence for the reader, not a gate: someone who writes a proposal by hand cannot leave it out."""
        pid = review.create(self.conn, "ai_edit", {"statements": [{"subject_id": self.a.source_id, "key": "title", "value": "x"}],
                                                  "claims": [{"text": "invented", "citations": [{"unit_version_id": self.uv, "quote": "nothing like this"}]}]}, "roy")
        self.refused(pid)

    def test_an_edit_that_writes_text_must_cite_evidence_even_if_the_proposer_omits_every_claim(self):
        """Found in the S2 review: a proposer that simply leaves out 'claims' escaped the citation rule. Writing a summary, a note or a unit's text
        without citing anything is refused; a retitle or a tag is a label, not a factual statement, and may go without."""
        for key in ("summary", "note"):
            fn = lambda c, key=key: {"statements": [{"subject_id": self.a.source_id, "key": key, "value": "The harbor is closed on Sundays."}]}      # noqa: E731
            pid = ai_edit.propose(self.conn, fn, self.a.source_id, "summarise", "roy")
            self.refused(pid)
        fn = lambda c: {"statements": [{"subject_id": self.uv, "key": "text", "value": "Rewritten with no evidence."}]}                          # noqa: E731
        self.refused(ai_edit.propose(self.conn, fn, self.uv, "rewrite", "roy"))
        retitle = ai_edit.propose(self.conn, self.proposer(), self.a.source_id, "retitle", "roy")
        review.approve(self.conn, retitle, "roy")                                       # a label needs no evidence

    def test_the_same_summary_with_a_real_citation_is_fine(self):
        fn = lambda c: {"statements": [{"subject_id": self.a.source_id, "key": "summary", "value": "A falcon over a harbor."}],               # noqa: E731
                        "claims": [self.good_claim()]}
        pid = ai_edit.propose(self.conn, fn, self.a.source_id, "summarise", "roy")
        review.approve(self.conn, pid, "roy")

    def test_a_real_citation_is_accepted_and_shown_on_the_approval_screen(self):
        pid = self.propose_with([self.good_claim()])
        screen = "\n".join(review.describe(self.conn, pid))
        self.assertIn("falcon flies over the harbor", screen)                       # the evidence sits next to the change
        self.assertIn("'Falcon Notes'", screen)
        self.assertNotIn("NOT FOUND", screen)
        review.approve(self.conn, pid, "roy")

    def test_a_claim_the_judge_doubts_needs_an_explicit_acknowledgement(self):
        """Support is a judgement: the user may overrule the judge, but not by accident."""
        claim = {"text": "The falcon migrates to Antarctica in winter.", "citations": [{"unit_version_id": self.uv, "quote": "falcon flies over the harbor"}]}
        pid = self.propose_with([claim])
        self.assertEqual(self.one("SELECT json_extract(diff_json, '$.checks.unsupported') FROM proposals WHERE proposal_id=?", pid)[0], "[0]")
        self.refused(pid)
        review.approve(self.conn, pid, "roy", acknowledge_unsupported=True)

    def test_a_better_judge_can_be_plugged_in_without_changing_the_rest(self):
        claim = {"text": "The falcon migrates to Antarctica in winter.", "citations": [{"unit_version_id": self.uv, "quote": "falcon flies over the harbor"}]}
        pid = ai_edit.propose(self.conn, self.proposer(claims=[claim]), self.a.source_id, "x", "roy", judge=lambda claim, evidence: True)
        review.approve(self.conn, pid, "roy")                                       # this judge sees no problem, so no acknowledgement is asked


class TestHiddenContentIsNotShownToAProposer(AiWorld):
    def test_nothing_hidden_reaches_a_proposer_or_becomes_the_subject_of_a_proposal(self):
        """Found in the S2 review: hiding a unit removed it from the index but its text was still offered to a proposer."""
        unit_id = self.one("SELECT unit_id FROM unit_versions WHERE unit_version_id=?", self.uv)[0]
        self.decide("delete", {"target_id": unit_id, "level": "a"})
        calls = []
        with self.assertRaises(ReviewRefused):
            ai_edit.propose(self.conn, lambda c: calls.append(c) or {"statements": []}, self.uv, "x", "roy")
        self.assertEqual(calls, [])                                                    # the proposer was never called
        for kind, diff in (("metadata", {"statements": [{"subject_id": self.uv, "key": "note", "value": "x"}]}),
                           ("patch", {"unit_version_id": self.uv, "op": "set_text", "new": "changed"})):
            with self.assertRaises(ReviewRefused, msg=kind):
                review.create(self.conn, kind, diff, "roy")

    def test_a_proposal_made_before_the_hiding_does_not_show_what_was_hidden_afterwards(self):
        """Round 2 of the S2 review: the approval screen of a PENDING proposal printed the id, the current value and the new text of a subject
        that had been hidden at level b in the meantime."""
        claim = self.good_claim()
        ai = ai_edit.propose(self.conn, self.proposer("Secret new title", claims=[claim]), self.a.source_id, "retitle", "roy")
        meta = review.create(self.conn, "metadata", {"statements": [{"subject_id": self.uv, "key": "note", "value": "zzcanarynote"}]}, "roy")
        patch = review.create(self.conn, "patch", {"unit_version_id": self.uv, "op": "set_text", "new": "zzcanarypatch text"}, "roy")
        before = {pid: "\n".join(review.describe(self.conn, pid)) for pid in (ai, meta, patch)}
        self.assertIn("falcon flies over the harbor", before[ai])
        self.assertIn("zzcanarynote", before[meta])
        unit_id = self.one("SELECT unit_id FROM unit_versions WHERE unit_version_id=?", self.uv)[0]
        review.apply(self.conn, (lambda pid: (review.approve(self.conn, pid, "roy"), pid)[1])(review.request_delete(self.conn, unit_id, "b", "roy")))
        for pid, text in ((meta, "zzcanarynote"), (patch, "zzcanarypatch")):
            screen = "\n".join(review.describe(self.conn, pid))
            self.assertNotIn(text, screen)
            self.assertNotIn(self.uv, screen)
            self.assertIn("hidden at level b", screen)
        screen = "\n".join(review.describe(self.conn, ai))
        self.assertNotIn("falcon flies over the harbor", screen)                      # the cited evidence is a hidden unit's text
        self.assertIn("kind=ai_edit", screen)                                           # the header (who, what kind) is not about the hidden thing

    def test_a_pending_delete_of_a_unit_does_not_show_the_unit_once_it_is_hidden_at_level_b(self):
        """The target of a delete proposal is a unit id (not a unit version): hidden_level has to recognise it by itself."""
        unit_id = self.one("SELECT unit_id FROM unit_versions WHERE unit_version_id=?", self.uv)[0]
        pending = review.request_delete(self.conn, unit_id, "a", "roy")
        self.assertIn("begins", "\n".join(review.describe(self.conn, pending)))
        review.approve(self.conn, (b := review.request_delete(self.conn, unit_id, "b", "roy")), "roy")
        review.apply(self.conn, b)
        screen = "\n".join(review.describe(self.conn, pending))
        self.assertNotIn("begins", screen)
        self.assertNotIn(unit_id, screen)
        self.assertIn("hidden at level b", screen)

    def test_a_hidden_source_is_not_shown_to_a_proposer_either(self):
        self.decide("delete", {"target_id": self.a.source_id, "level": "a"})
        with self.assertRaises(ReviewRefused):
            ai_edit.propose(self.conn, self.proposer(), self.a.source_id, "x", "roy")


class TestHumanContentIsProtected(AiWorld):
    def setUp(self):
        super().setUp()
        self.decide("metadata", {"statements": [{"subject_id": self.a.source_id, "key": "title", "value": "Title by Roy"}]})
        self.build()

    def ai_title(self, value="Title by the model"):
        return ai_edit.propose(self.conn, self.proposer(value), self.a.source_id, "retitle", "roy")

    def test_an_ai_edit_cannot_replace_human_content_unless_the_user_saw_the_difference_and_said_so(self):
        pid = self.ai_title()
        review.approve(self.conn, pid, "roy")                                       # a plain approval is not an override
        with self.assertRaises(ReviewRefused):
            review.apply(self.conn, pid)
        self.assertEqual(self.one("SELECT state FROM proposals WHERE proposal_id=?", pid)[0], "approved")
        self.assertEqual(self.one("SELECT count(*) FROM statements WHERE origin='ai'")[0], 0)
        self.build()
        self.assertEqual(self.displayed(self.a.source_id, "title"), ("Title by Roy", "human"))

    def test_with_the_override_flag_the_ai_edit_replaces_it(self):
        pid = self.ai_title()
        review.approve(self.conn, pid, "roy", override_protected=True)
        review.apply(self.conn, pid)
        self.add("alpha.md", "# Alpha\n\nThe falcon flies over the harbor at dawn.\n\nKelp forests grow offshore.\n\nOne more line.\n")
        self.build()
        self.assertEqual(self.displayed(self.a.source_id, "title"), ("Title by the model", "ai"))

    def test_the_override_flag_is_for_ai_edits_only(self):
        pid = review.create(self.conn, "metadata", {"statements": [{"subject_id": self.a.source_id, "key": "note", "value": "x"}]}, "roy")
        with self.assertRaises(ReviewRefused):
            review.approve(self.conn, pid, "roy", override_protected=True)

    def test_the_approval_screen_shows_the_current_and_the_new_value(self):
        screen = "\n".join(review.describe(self.conn, self.ai_title()))
        self.assertIn("'Title by Roy'", screen)
        self.assertIn("'Title by the model'", screen)


class TestSupportJudgeEvaluation(unittest.TestCase):
    """The harness that measures a judge on labelled samples (Gate 2 T-AI1). The baseline numbers are a snapshot, recorded in the S2 report."""

    def samples(self):
        with open(SAMPLES, encoding="utf-8") as f:
            return json.load(f)["samples"]

    def test_the_samples_cover_the_four_cases(self):
        labels = [s["label"] for s in self.samples()]
        self.assertEqual({lab: labels.count(lab) for lab in evidence.LABELS}, {lab: 5 for lab in evidence.LABELS})

    def test_a_perfect_judge_scores_one_and_the_two_error_types_are_counted_apart(self):
        s = self.samples()
        perfect = lambda claim, ev: next(x["label"] in evidence.SUPPORTED_LABELS for x in s if x["claim"] == claim)       # noqa: E731
        self.assertEqual(evidence.evaluate_judge(perfect, s)["accuracy"], 1.0)
        yes = evidence.evaluate_judge(lambda c, e: True, s)
        self.assertEqual((yes["wrongly_accepted"], yes["wrongly_flagged"]), (5, 0))
        no = evidence.evaluate_judge(lambda c, e: False, s)
        self.assertEqual((no["wrongly_accepted"], no["wrongly_flagged"]), (0, 15))

    def test_the_baseline_overlap_judge_is_a_weak_aid_not_a_gate(self):
        """Snapshot: it accepts a claim that changes one word of the evidence, and it flags every faithful paraphrase. That is why support never
        blocks an approval by itself and why existence (evidence.missing_citations) is the hard rule."""
        r = evidence.evaluate_judge(evidence.overlap_judge, self.samples())
        self.assertEqual(r["accuracy"], 0.55)
        self.assertEqual(r["per_label"]["supports"]["correct"], 5)
        self.assertEqual(r["per_label"]["paraphrase_supports"]["correct"], 0)
        self.assertEqual((r["wrongly_accepted"], r["wrongly_flagged"]), (1, 8))


if __name__ == "__main__":
    unittest.main()
