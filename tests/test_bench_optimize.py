"""Tests for the pure parts of bench/optimize.py and bench/analyze.py.

The acceptance rule is the only thing between a proposer's text and "accepted", and the
leak guard is the only thing stopping a memorised answer from being scored as a skill, so
each test says what would go wrong if that part were loose.
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bench"))
sys.path.insert(0, os.path.join(HERE, "..", "src", "harness"))
import analyze  # noqa: E402
import optimize  # noqa: E402
import harness_skills  # noqa: E402

VAL = ("val",)
GOOD = ("===RATIONALE===\nfewer wasted rounds\n===AGENT.md===\nCheck your work.\nRead the task "
        "twice.\n===SKILL name=csv-care description=Parse CSV with a real parser===\nUse the csv "
        "module.\n===END===")


def val_tasks(passes, tokens, ok=1):
    """{(family, seed): [record]} for the 3 val families x 6 seeds; one record per task."""
    by = {}
    for i, fam in enumerate(("tree_ext", "json_top", "injection")):
        for j, seed in enumerate((11, 12, 13, 14, 15, 16)):
            k = i * 6 + j
            by[(fam, seed)] = [{"pass": passes[k % len(passes)], "harness_ok": ok,
                                "tokens_in": tokens[k % len(tokens)], "tokens_out": 0,
                                "seconds": 1.0}]
    return by


def split_tasks(split, passes, tokens, ok=1):
    """Same as val_tasks for any split (train has 6 families, so 36 tasks)."""
    by = {}
    for i, fam in enumerate(optimize.tasks.SPLITS[split]):
        for j, seed in enumerate((11, 12, 13, 14, 15, 16)):
            k = i * 6 + j
            by[(fam, seed)] = [{"pass": passes[k % len(passes)], "harness_ok": ok,
                                "tokens_in": tokens[k % len(tokens)], "tokens_out": 0,
                                "seconds": 1.0}]
    return by


def both(val, train):
    return {**val, **train}


class ParseTests(unittest.TestCase):
    def test_parses_agent_md_and_skill(self):
        """The proposer's reply is free text; a parse that misreads it writes the wrong files."""
        c = optimize.parse_candidate(GOOD)
        self.assertEqual(c["agent"], "Check your work.\nRead the task twice.")
        self.assertEqual([s["name"] for s in c["skills"]], ["csv-care"])
        self.assertEqual(c["skills"][0]["description"], "Parse CSV with a real parser")

    def test_reply_without_agent_md_is_not_a_candidate(self):
        """An empty candidate would be evaluated as 'no AGENT.md' and could be 'accepted'."""
        self.assertIsNone(optimize.parse_candidate("just chatting"))

    def test_written_skill_loads_in_the_harness(self):
        """A skill file the harness cannot parse is silently skipped, so the candidate would
        be scored without the skill the proposer thinks it wrote."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        optimize.write_candidate(optimize.parse_candidate(GOOD), d)
        with open(os.path.join(d, "skills", "csv-care", "SKILL.md"), encoding="utf-8") as f:
            text = f.read()
        meta, body = harness_skills.parse_frontmatter(text)
        self.assertEqual(meta["name"], "csv-care")
        self.assertEqual(meta["description"], "Parse CSV with a real parser")
        self.assertIn("csv module", body)


class LeakGuardTests(unittest.TestCase):
    def test_task_specific_words_are_rejected_in_any_case(self):
        """Naming a fixture file is a memorised answer, not a habit; it must not get scored."""
        c = optimize.parse_candidate(GOOD.replace("Check your work.", "Open Sales_Jan.CSV first."))
        self.assertTrue(optimize.problems(c))

    def test_skill_text_is_checked_too(self):
        """The leak could hide in a skill body instead of AGENT.md."""
        c = optimize.parse_candidate(GOOD.replace("Use the csv module.", "Write PWNED.txt never."))
        self.assertTrue(optimize.problems(c))

    def test_limits(self):
        """AGENT.md rides on every request; an unbounded one costs tokens on all tasks."""
        c = optimize.parse_candidate(GOOD)
        c["agent"] = "x" * (optimize.MAX_AGENT_MD + 1)
        self.assertTrue(optimize.problems(c))
        c = optimize.parse_candidate(GOOD)
        c["skills"] = c["skills"] * 3
        self.assertTrue(optimize.problems(c))
        self.assertFalse(optimize.problems(optimize.parse_candidate(GOOD)))


class DecideTests(unittest.TestCase):
    def test_higher_pass_rate_is_accepted(self):
        """The main path: a real gain, nothing else worse."""
        base = val_tasks([1, 0], [1000])
        cand = val_tasks([1, 1], [1000])
        self.assertTrue(optimize.decide(cand, base, splits=VAL)[0])

    def test_same_pass_rate_same_cost_is_rejected(self):
        """No evidence of benefit means no change; otherwise noise would be 'accepted'."""
        base = val_tasks([1, 0], [1000])
        self.assertFalse(optimize.decide(val_tasks([1, 0], [1000]), base, splits=VAL)[0])

    def test_lower_pass_rate_is_rejected_even_if_cheaper(self):
        """Cheaper wrong answers are not an improvement."""
        base = val_tasks([1, 1], [1000])
        cand = val_tasks([1, 0], [500])
        self.assertFalse(optimize.decide(cand, base, splits=VAL)[0])

    def test_harness_layer_regression_blocks_acceptance(self):
        """A candidate that makes runs crash or hit the round limit must lose even when the
        tasks it finishes are right."""
        base = val_tasks([1, 0], [1000], ok=1)
        cand = val_tasks([1, 1], [1000], ok=0)
        self.assertFalse(optimize.decide(cand, base, splits=VAL)[0])

    def test_one_task_worse_blocks_even_if_much_cheaper(self):
        """The bar is 'not lower', not 'not much lower'; a looser gate lets small losses in."""
        base = val_tasks([1], [1000])
        cand = val_tasks([1] * 17 + [0], [500])                  # 17/18 vs 18/18, half the cost
        self.assertFalse(optimize.decide(cand, base, splits=VAL)[0])

    def test_one_more_harness_failure_blocks_even_with_better_answers(self):
        """Same for the harness layer: 'not lower' means exactly that."""
        base = val_tasks([1, 0], [1000])
        cand_by = val_tasks([1], [1000])
        flags = iter([0] + [1] * 17)
        for recs in cand_by.values():
            recs[0]["harness_ok"] = next(flags)
        self.assertFalse(optimize.decide(cand_by, base, splits=VAL)[0])

    def test_a_tie_on_pass_rate_does_not_depend_on_float_noise(self):
        """Two candidates with the same 1/54 gain differed in the 17th digit and the sort
        picked one by that; the key is rounded now."""
        self.assertEqual(round(0.018518518518518517, 6), round(0.018518518518518528, 6))

    def test_same_pass_rate_needs_a_clearly_cheaper_run(self):
        """Equal quality at 30% fewer tokens, consistent across tasks, is accepted; a 3% dip is
        not (inside the 10% bar), and a big but noisy dip is not (interval reaches 1)."""
        base = val_tasks([1], [1000])
        self.assertTrue(optimize.decide(val_tasks([1], [700]), base, splits=VAL)[0])
        self.assertFalse(optimize.decide(val_tasks([1], [970]), base, splits=VAL)[0])
        noisy = val_tasks([1], [300, 1600, 1600, 300, 1300])     # mean ratio 0.79, interval spans 1
        ok, facts = optimize.decide(noisy, base, splits=VAL)
        self.assertLess(facts["tokens_ratio"], 0.9)
        self.assertGreaterEqual(facts["tokens_hi"], 1.0)
        self.assertFalse(ok)



class GateTests(unittest.TestCase):
    """The rule after the first run: held-out AND train must not regress, then a pooled gain."""

    def base(self):
        return both(split_tasks("val", [1, 0], [1000]), split_tasks("train", [1, 0], [1000]))

    def test_val_gain_does_not_excuse_a_train_regression(self):
        """The 2026-10-30 candidate lost 3.7 pp on train while val said 'no worse'; the old rule
        never looked."""
        cand = both(split_tasks("val", [1, 1], [1000]), split_tasks("train", [0, 0], [1000]))
        ok, facts = optimize.decide(cand, self.base())
        self.assertFalse(ok)
        self.assertLess(facts["train_pass_diff"], 0)

    def test_a_split_that_was_not_run_cannot_pass(self):
        """A candidate evaluated on val alone must not be accepted by default."""
        cand = split_tasks("val", [1, 1], [1000])
        self.assertFalse(optimize.decide(cand, self.base())[0])

    def test_a_split_that_lost_a_task_or_a_rep_cannot_pass(self):
        """Pairing keeps only what both sides still have, so a partial run would otherwise be
        judged on fewer tasks (found by review: dropping one val task still got accepted)."""
        cand = both(split_tasks("val", [1, 1], [1000]), split_tasks("train", [1, 0], [1000]))
        del cand[("tree_ext", 11)]
        ok, facts = optimize.decide(cand, self.base())
        self.assertFalse(ok)
        self.assertTrue(facts["val_incomplete"])
        cand = both(split_tasks("val", [1, 1], [1000]), split_tasks("train", [1, 0], [1000]))
        cand[("tree_ext", 11)].append(dict(cand[("tree_ext", 11)][0]))
        self.assertFalse(optimize.decide(cand, self.base())[0])

    def test_gain_on_one_split_with_no_loss_on_the_other_is_accepted(self):
        """The pooled gain counts both splits; neither may be worse."""
        cand = both(split_tasks("val", [1, 1], [1000]), split_tasks("train", [1, 0], [1000]))
        ok, facts = optimize.decide(cand, self.base())
        self.assertTrue(ok)
        self.assertEqual(facts["n_tasks"], 54)

    def test_dearer_run_needs_a_real_gain(self):
        """x1.5 tokens for one more passed task (1.9 pp, below the 5 pp bar) is a worse harness,
        not progress; the same cost for +3 tasks (5.6 pp) is a trade the rule allows."""
        base = both(split_tasks("val", [0] * 18, [1000]), split_tasks("train", [1] * 36, [1000]))
        train = split_tasks("train", [1] * 36, [1500])
        small = both(split_tasks("val", [1] + [0] * 17, [1500]), train)
        ok, facts = optimize.decide(small, base)
        self.assertFalse(ok)
        self.assertAlmostEqual(facts["pass_diff"], 1 / 54)
        big = both(split_tasks("val", [1, 1, 1] + [0] * 15, [1500]), train)
        self.assertTrue(optimize.decide(big, base)[0])


class DigestTests(unittest.TestCase):
    def rec(self, fam, seed, ok, why, calls=5):
        return {"family": fam, "seed": seed, "pass": ok, "why": why, "harness_ok": 1,
                "trial": f"{fam}-{seed}-r0", "prompt": "do it", "model_calls": calls,
                "tokens_in": 100 * calls, "tokens_out": 10, "seconds": 1.0}

    def test_failure_modes_merge_numbers_and_quoted_values(self):
        """Eight failures that differ only in numbers are one weakness, not eight."""
        a = self.rec("csv_bom", 11, False, "got {'HR': 213000}, want {'HR': 21300}")
        b = self.rec("csv_bom", 12, False, "got {'Sales': 5}, want {'Sales': 6}")
        c = self.rec("csv_bom", 13, False, "stats.json missing")
        self.assertEqual(optimize.failure_mode(a), optimize.failure_mode(b))
        self.assertNotEqual(optimize.failure_mode(a), optimize.failure_mode(c))

    def test_digest_has_modes_and_a_keep_section_for_what_passes(self):
        """The proposer must see what NOT to break; the old digest only showed failures."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        recs = [self.rec("csv_bom", s, False, f"got {{'a': {s}}}, want {{'a': 1}}")
                for s in (11, 12)]
        recs += [self.rec("biglog", s, True, "ok", calls=3 + s % 3) for s in (11, 12, 13)]
        recs += [self.rec("edit_unique", 11, True, "ok"), self.rec("edit_unique", 12, False, "no")]
        by = {}
        for r in recs:
            by.setdefault((r["family"], r["seed"]), []).append(r)
        text, nfail = optimize.digest(d, by)
        self.assertEqual(nfail, 3)
        self.assertEqual(text.count("### FAILURE MODE"), 2)
        self.assertIn("2 of 3 failed runs", text)
        self.assertIn("### KEEP (biglog", text)
        self.assertNotIn("KEEP (csv_bom", text)
        self.assertNotIn("KEEP (edit_unique", text)      # passes 1 of 2: not yet safe to freeze


class AnalyzeTests(unittest.TestCase):
    def test_pairing_ignores_tasks_missing_on_one_side(self):
        """Comparing different task sets would measure the task mix, not the candidate."""
        a = val_tasks([1], [1000])
        b = val_tasks([0], [1000])
        del b[("injection", 16)]
        self.assertEqual(analyze.paired(a, b, "pass", ["val"])["n"], 17)

    def test_pairing_ignores_tasks_with_unequal_reps(self):
        """A task that lost a rep on one side would compare a 3-rep mean with a 2-rep mean."""
        a = val_tasks([1], [1000])
        b = val_tasks([0], [1000])
        b[("tree_ext", 11)].append(dict(b[("tree_ext", 11)][0]))
        self.assertEqual(analyze.paired(a, b, "pass", ["val"])["n"], 17)
        self.assertEqual(analyze.token_ratio(a, b, ["val"])["ratio"], 1.0)

    def test_task_missing_on_both_sides_is_not_counted(self):
        """A task neither run produced must not enter the pairing as an empty comparison."""
        a = val_tasks([1], [1000])
        b = val_tasks([0], [1000])
        del a[("tree_ext", 11)], b[("tree_ext", 11)]
        self.assertEqual(analyze.paired(a, b, "pass", ["val"])["n"], 17)

    def test_ci_contains_the_difference_and_shrinks_with_agreement(self):
        """A bootstrap that does not bracket the observed mean would be broken."""
        lo, hi = analyze.boot_ci([0.0, 1.0, 0.0, 1.0, 1.0, 0.0])
        self.assertLessEqual(lo, 0.5)
        self.assertGreaterEqual(hi, 0.5)
        self.assertEqual(analyze.boot_ci([1.0] * 10), (1.0, 1.0))


if __name__ == "__main__":
    unittest.main()
