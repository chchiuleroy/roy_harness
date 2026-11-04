"""Slice 4, S0: the measuring functions. Pure, so every expected value here is worked out by hand.

Each test's docstring says WHY the behaviour matters.
"""
import math
import unittest

import superagent_support as sup  # noqa: F401  (puts src/ on the path)
from superagent import measure as m
from superagent.measure import NULL, UNAVAILABLE

Z95 = 1.959963984540054        # the usual constant, written out so the check does not use the code under test


class TestWilson(unittest.TestCase):
    def test_all_successes_have_a_closed_form_lower_bound(self):
        """With k == n the lower bound is n / (n + z^2) and the upper is 1. A Wald interval would give
        [1, 1], which claims certainty from ten rows."""
        lo, hi = m.wilson(10, 10)
        self.assertAlmostEqual(lo, 10 / (10 + Z95 ** 2), places=12)
        self.assertEqual(hi, 1.0)
        self.assertAlmostEqual(lo, 0.72247, places=5)

    def test_no_successes_have_a_closed_form_upper_bound(self):
        lo, hi = m.wilson(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, Z95 ** 2 / (10 + Z95 ** 2), places=12)

    def test_half_and_half_is_symmetric_around_one_half(self):
        lo, hi = m.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.23659, places=5)
        self.assertAlmostEqual(hi, 0.76341, places=5)
        self.assertAlmostEqual(lo + hi, 1.0, places=12)

    def test_a_larger_sample_gives_a_narrower_interval(self):
        widths = [m.half_width(m.wilson(n // 2, n)) for n in (10, 40, 160, 640)]
        self.assertEqual(widths, sorted(widths, reverse=True))

    def test_a_higher_confidence_gives_a_wider_interval(self):
        self.assertGreater(m.half_width(m.wilson(7, 10, 0.99)), m.half_width(m.wilson(7, 10, 0.95)))
        self.assertGreater(m.half_width(m.wilson(7, 10, 0.95)), m.half_width(m.wilson(7, 10, 0.80)))

    def test_no_rows_means_nothing_to_estimate(self):
        """n == 0 must not come out as 0 or 1: there is no estimate at all."""
        self.assertIsNone(m.wilson(0, 0))
        self.assertEqual(m.proportion(0, 0), {"k": 0, "n": 0, "estimate": None, "interval": None})

    def test_bad_input_is_refused(self):
        for args in ((3, 2), (-1, 5), (1.5, 4), (True, 4), (1, True)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    m.wilson(*args)
        for conf in (0, 1, -0.1, 1.5, float("nan"), float("inf"), True, "0.95"):
            with self.subTest(confidence=conf):
                with self.assertRaises(ValueError):
                    m.wilson(1, 2, conf)


class TestRowsNeeded(unittest.TestCase):
    @staticmethod
    def hw(p, n):
        z = Z95
        denom = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / denom
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
        return (min(1.0, centre + half) - max(0.0, centre - half)) / 2

    def test_it_matches_a_plain_scan_upwards(self):
        """The search is a doubling plus bisection; a linear scan is the obviously correct version."""
        for k, n, h in ((9, 10, 0.1), (18, 20, 0.05), (5, 10, 0.1), (0, 10, 0.05), (10, 10, 0.02), (30, 60, 0.07)):
            with self.subTest(k=k, n=n, h=h):
                want = n
                while self.hw(k / n, want) > h:
                    want += 1
                self.assertEqual(m.rows_needed(k, n, h), want)

    def test_if_the_target_is_already_met_it_asks_for_no_more(self):
        self.assertEqual(m.rows_needed(500, 1000, 0.1), 1000)

    def test_it_never_asks_for_fewer_rows_than_there_are(self):
        self.assertGreaterEqual(m.rows_needed(1, 3, 0.4), 3)

    def test_nothing_to_extrapolate_from_without_rows(self):
        self.assertIsNone(m.rows_needed(0, 0, 0.1))

    def test_bad_targets_are_refused(self):
        for h in (0, -0.1, 0.51, float("nan"), float("inf"), True, "0.1"):
            with self.subTest(h=h):
                with self.assertRaises(ValueError):
                    m.rows_needed(5, 10, h)


def rows(*pairs):
    return [{"label": a, "outcome": b} for a, b in pairs]


# A table worked out by hand (rules a and b):
#   1 label a -> a   2 label a -> b   3 label a -> null   4 label a -> unavailable   5 label b -> b
#   6 none -> null   7 none -> a      8 none -> unavailable                          9 none -> null
TABLE = rows(("a", "a"), ("a", "b"), ("a", NULL), ("a", UNAVAILABLE), ("b", "b"),
             (None, NULL), (None, "a"), (None, UNAVAILABLE), (None, NULL))


def kn(p):
    return p["k"], p["n"]


class TestScore(unittest.TestCase):
    def setUp(self):
        self.s = m.score(TABLE, ["a", "b"])
        self.p, self.d = self.s["primary"], self.s["diagnostic"]

    def test_primary_counts_match_the_hand_worked_table(self):
        self.assertEqual((self.p["rows"], self.p["positives"], self.p["negatives"], self.p["predicted_positive"]),
                         (9, 5, 4, 4))
        self.assertEqual(kn(self.p["recall"]), (3, 5))          # rows 1, 2, 5 of the five positives
        self.assertEqual(kn(self.p["precision"]), (3, 4))       # rows 1, 2, 5, 7 were called positive
        self.assertEqual(kn(self.p["specificity"]), (2, 4))     # rows 6, 9; row 8 (declined) does not count
        self.assertEqual(kn(self.p["availability"]), (7, 9))    # rows 4 and 8 have no answer
        self.assertEqual(kn(self.p["exact_rule_accuracy"]), (2, 3))   # rows 1, 5 of rows 1, 2, 5

    def test_a_declined_positive_is_a_miss_in_the_main_result(self):
        """Intention to classify: row 4 was sent to the model and got no answer, so recall pays for it."""
        self.assertEqual(kn(self.p["recall"]), (3, 5))

    def test_the_diagnostic_block_only_looks_at_answered_rows(self):
        self.assertEqual((self.d["rows"], self.d["positives"], self.d["negatives"]), (7, 4, 3))
        self.assertEqual(kn(self.d["recall"]), (3, 4))
        self.assertEqual(kn(self.d["precision"]), (3, 4))
        self.assertEqual(kn(self.d["specificity"]), (2, 3))
        self.assertNotIn("availability", self.d)

    def test_per_rule_and_micro_are_one_against_the_rest(self):
        """Row 2 (label a, answered b) is a miss for a and a false alarm for b."""
        a, b = self.p["per_rule"]["a"], self.p["per_rule"]["b"]
        self.assertEqual((a["labelled"], a["predicted"], kn(a["precision"]), kn(a["recall"])), (4, 2, (1, 2), (1, 4)))
        self.assertEqual((b["labelled"], b["predicted"], kn(b["precision"]), kn(b["recall"])), (1, 2, (1, 2), (1, 1)))
        self.assertEqual(kn(self.p["micro"]["precision"]), (2, 4))
        self.assertEqual(kn(self.p["micro"]["recall"]), (2, 5))

    def test_a_model_that_declines_everything_cannot_look_good(self):
        """Why precision is never read alone: here it has nothing to say, but recall is zero and
        availability is zero."""
        s = m.score(rows(("a", UNAVAILABLE), ("a", UNAVAILABLE), (None, UNAVAILABLE)), ["a"])["primary"]
        self.assertEqual(kn(s["recall"]), (0, 2))
        self.assertIsNone(s["precision"]["estimate"])
        self.assertEqual(kn(s["precision"]), (0, 0))
        self.assertEqual(kn(s["specificity"]), (0, 1))
        self.assertEqual(kn(s["availability"]), (0, 3))

    def test_a_corpus_with_no_positives_has_no_recall(self):
        s = m.score(rows((None, NULL), (None, "a")), ["a"])["primary"]
        self.assertEqual(s["recall"], {"k": 0, "n": 0, "estimate": None, "interval": None})
        self.assertEqual(kn(s["precision"]), (0, 1))

    def test_unknown_labels_and_outcomes_are_refused(self):
        with self.assertRaises(ValueError):
            m.score(rows(("zzz", NULL)), ["a"])
        with self.assertRaises(ValueError):
            m.score(rows((None, "zzz")), ["a"])

    def test_every_proportion_has_a_wilson_interval(self):
        iv = self.p["recall"]["interval"]
        self.assertEqual(iv, list(m.wilson(3, 5)))


class TestPrecisionOfEstimate(unittest.TestCase):
    def test_it_says_which_estimates_are_narrow_enough_and_how_many_rows_would_do(self):
        s = m.score(TABLE, ["a", "b"])["primary"]
        out = m.precision_of_estimate(s, 0.10)
        self.assertEqual(out["per_metric"]["recall"]["met"], False)          # 5 rows cannot give +/-0.10
        self.assertGreater(out["per_metric"]["recall"]["rows_needed"], 5)
        self.assertIn("rows_needed", out["assumption"])

    def test_the_conclusion_never_says_the_model_is_good(self):
        """The tool measures precision of estimation, not quality: no pass/fail wording may creep in."""
        out = m.precision_of_estimate(m.score(TABLE, ["a", "b"])["primary"], 0.10)
        text = " ".join(out["statement"]).lower()
        self.assertIn("no acceptance threshold", text)
        for word in ("pass", "fail", "approved", "good enough to"):
            self.assertNotIn(word, text.replace("this report does not say the model is good enough", ""))

    def test_a_metric_without_data_is_reported_as_such(self):
        out = m.precision_of_estimate(m.score(rows((None, NULL)), ["a"])["primary"], 0.10)
        self.assertIsNone(out["per_metric"]["recall"]["met"])
        self.assertIn("recall", out["statement"][0])


class TestStability(unittest.TestCase):
    def test_one_run_has_nothing_to_compare(self):
        self.assertEqual(m.stability([["a", "b"]]), {"repeats": 1, "available": False})

    def test_agreement_across_repeats(self):
        out = m.stability([["a", NULL, "b"], ["a", "a", "b"], ["a", NULL, UNAVAILABLE]])
        self.assertEqual(out["fully_consistent_rows"], 1)             # only the first row never changed
        self.assertAlmostEqual(out["fully_consistent_share"], 1 / 3)
        self.assertAlmostEqual(out["mean_modal_share"], (1 + 2 / 3 + 2 / 3) / 3)

    def test_no_interval_is_offered(self):
        """Repeats of the same rows are not independent samples; an interval would overstate the evidence."""
        self.assertNotIn("interval", m.stability([["a"], ["a"]]))

    def test_runs_must_cover_the_same_rows(self):
        with self.assertRaises(ValueError):
            m.stability([["a"], ["a", "b"]])


class TestOrderSensitivity(unittest.TestCase):
    def test_share_of_rows_that_changed(self):
        out = m.order_sensitivity(["a", NULL, "b", "a"], ["a", "a", "b", NULL], [False] * 4)
        self.assertEqual((out["rows"], out["changed"]["k"], out["changed"]["n"]), (4, 2, 4))
        self.assertIsNone(out["baseline"])

    def test_a_row_alone_in_its_batch_is_left_out(self):
        """Reversing a batch of one changes nothing, so counting it would dilute the share."""
        out = m.order_sensitivity(["a", NULL, "b"], ["a", "a", "b"], [True, False, False])
        self.assertEqual((out["rows"], out["changed"]["k"]), (2, 1))

    def test_the_baseline_is_a_noise_reference_over_the_same_rows(self):
        out = m.order_sensitivity(["a", NULL, "b"], ["a", "a", "a"], [False, False, False], baseline=["a", NULL, "a"])
        self.assertEqual((out["changed"]["k"], out["baseline"]["k"], out["baseline"]["n"]), (2, 1, 3))

    def test_it_does_not_call_itself_a_position_effect(self):
        out = m.order_sensitivity(["a"], ["a"], [False])
        self.assertIn("not a position effect", out["note"])

    def test_lengths_must_agree(self):
        with self.assertRaises(ValueError):
            m.order_sensitivity(["a"], ["a", "b"], [False])


if __name__ == "__main__":
    unittest.main()
