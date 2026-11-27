"""tools/mutate_check.py: a mutation checker is only worth having if it can say "survived".

Each test builds a tiny throwaway project and checks the verdict. If the tool always said "killed", every mutant
list would look perfect and prove nothing; these tests are what stops that."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import mutate_check as mc  # noqa: E402

MOD = "def f():\n    return 1\n\ndef g():\n    return 1\n"
TEST = ("import os, sys, unittest\n"
        "sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))\n"
        "import mod\n"
        "class TestMod(unittest.TestCase):\n"
        "    def test_f(self):\n"
        "        self.assertEqual(mod.f(), 1)\n")


def toy(test_text=TEST):
    root = tempfile.mkdtemp(prefix="mc_toy_")
    for rel, text in (("src/mod.py", MOD), ("tests/test_mod.py", test_text)):
        os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
        with open(os.path.join(root, rel), "w", encoding="utf-8", newline="") as f:
            f.write(text)
    return root


def verdict(old, new, test_text=TEST, timeout=60):
    root = toy(test_text)
    return mc.check_mutant(("m", "src/mod.py", old, new, "test_mod.TestMod.test_f"), root=root, timeout=timeout)[1]


class TestMutateCheck(unittest.TestCase):
    def test_a_change_the_test_notices_is_killed(self):
        self.assertEqual(verdict("def f():\n    return 1", "def f():\n    return 2"), "killed")

    def test_a_change_the_test_cannot_see_survives(self):
        """`g` is never called by the test: breaking it must be reported, not hidden."""
        self.assertEqual(verdict("def g():\n    return 1", "def g():\n    return 2"), "SURVIVED")

    def test_an_old_text_that_is_not_unique_is_refused(self):
        """`return 1` occurs twice; replacing 'the first one' silently would make the verdict about a guess."""
        self.assertEqual(verdict("return 1", "return 2"), "invalid")

    def test_a_mutant_that_does_not_compile_is_not_counted_as_killed(self):
        """A syntax error makes any test fail; that says nothing about the test's quality."""
        self.assertEqual(verdict("def f():\n    return 1", "def f(:\n    return 1"), "invalid")

    def test_a_test_that_fails_before_the_change_proves_nothing(self):
        broken = TEST.replace("mod.f(), 1", "mod.f(), 99")
        self.assertEqual(verdict("def f():\n    return 1", "def f():\n    return 2", broken), "baseline-failed")

    def test_a_crash_that_is_not_a_failed_assertion_is_not_counted_as_killed(self):
        """A mutant that makes the test die with NameError (or an import error) turns it red, but that says the
        mutant is broken, not that the test checks the behaviour. Reviewer finding, 2026-11-27."""
        self.assertEqual(verdict("def f():\n    return 1", "def f():\n    return undefined_name"), "errored")

    def test_a_hang_before_any_change_is_a_baseline_failure_not_a_kill(self):
        """If the untouched test already hangs, 'it timed out after the change' proves nothing. Found by the
        review of this tool: the baseline timeout used to be reported as killed."""
        slow = TEST.replace("import mod\n", "import mod, time\ntime.sleep(30)\n")
        self.assertEqual(verdict("def f():\n    return 1", "def f():\n    return 2", slow, timeout=2), "baseline-failed")

    def test_every_listed_mutant_names_an_existing_file_and_test_function(self):
        """A typo in the list would otherwise show up only as a baseline failure mid-run."""
        names = [m[0] for m in mc.MUTANTS]
        self.assertEqual(len(names), len(set(names)), "duplicate mutant names")
        for name, rel, old, new, test_id in mc.MUTANTS:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), f"{name}: no file {rel}")
            module, cls, func = test_id.split(".")
            with open(os.path.join(HERE, module + ".py"), encoding="utf-8") as f:
                self.assertIn(f"def {func}(", f.read(), f"{name}: no test {test_id}")


if __name__ == "__main__":
    unittest.main()
