"""R1: dependency direction, by syntax-tree analysis (Gate 2 OPT1). The analysers are themselves tested on synthetic trees:
a checker that has never been seen to fail proves nothing."""
import os
import sys
import tomllib
import unittest

from kb import layers

from .support import SRC, tmpdir

ROOT = os.path.dirname(SRC)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import importgraph  # noqa: E402

PKG = os.path.join(SRC, "kb")
HARNESS_DIR = os.environ.get("KB_HARNESS_DIR", "C:/Users/roy/roy_harness/src/harness")


def write_tree(testcase, files):
    d = tmpdir(testcase)
    for name, text in files.items():
        with open(os.path.join(d, name), "w", encoding="utf-8", newline="") as f:
            f.write(text)
    return d


class TestKbLayering(unittest.TestCase):
    def test_real_package_respects_the_tiers(self):
        """Lower tiers must never import higher ones; every module must declare its tier."""
        self.assertEqual(importgraph.check_kb_layers(PKG, layers.MODULES, layers.import_allowed), [])

    def test_core_tier_is_standard_library_only(self):
        """The optional-install promise: core works with nothing installed beyond Python."""
        self.assertEqual(importgraph.third_party_in_core(PKG, layers.MODULES), [])

    def test_pyproject_declares_no_runtime_dependencies_and_no_harness(self):
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
            cfg = tomllib.load(f)
        self.assertEqual(cfg["project"]["dependencies"], [])
        text = repr(cfg).lower()
        self.assertNotIn("harness", text)

    def test_import_rule_truth_table(self):
        ok, no = layers.import_allowed, lambda a, b: not layers.import_allowed(a, b)
        self.assertTrue(ok("T0", "T0") and ok("T1", "T0") and ok("T2", "T0") and ok("T3", "T1") and ok("APP", "T3") and ok("EVAL", "T3"))
        self.assertTrue(no("T0", "T1") and no("T0", "T3") and no("T1", "T2") and no("T2", "T1") and no("T3", "APP") and no("T3", "EVAL"))

    def test_checker_flags_forbidden_edges_unmapped_and_stale_modules(self):
        """The checker must go red on each kind of violation, including dynamic imports."""
        d = write_tree(self, {
            "low.py": "from .high import x\n",
            "high.py": "import json\n",
            "dyn.py": "import importlib\nimportlib.import_module('kb.high')\n",
            "extra.py": "x = 1\n",
        })
        mods = {"low": "T0", "high": "T3", "dyn": "T0", "ghost": "T0"}
        problems = importgraph.check_kb_layers(d, mods, layers.import_allowed)
        kinds = {p[0] for p in problems}
        self.assertEqual(kinds, {"forbidden_edge", "unmapped_module", "stale_table_entry"})
        self.assertIn(("forbidden_edge", "low(T0) -> high(T3)"), problems)
        self.assertIn(("forbidden_edge", "dyn(T0) -> high(T3)"), problems)

    def test_core_checker_flags_third_party(self):
        d = write_tree(self, {"core.py": "import numpy\nimport json\n"})
        self.assertEqual(importgraph.third_party_in_core(d, {"core": "T0"}), [("core", "numpy")])


class TestHarnessDoesNotKnowKb(unittest.TestCase):
    def test_scanner_flags_unanalysable_code_and_aliased_dynamic_imports(self):
        """The scanner is a guard for known syntactic forms: aliased dynamic imports are resolved; what it cannot analyse
        (exec/eval/compile, imports of a computed name) is reported as a finding rather than silently passed."""
        bad = {
            "a.py": "import importlib as il\nil.import_module('kb.search')\n",
            "b.py": "from importlib import import_module as im\nim('kb')\n",
            "c.py": "name = 'k' + 'b'\n__import__(name)\n",
            "d.py": "exec('import kb')\n", "e.py": "eval('1')\n", "f.py": "x = compile('1', 'f', 'eval')\n",
        }
        by_file = {p[0]: p[1] for p in importgraph.scan_harness(write_tree(self, bad))}
        self.assertEqual(by_file["a.py"], "imports")
        self.assertEqual(by_file["b.py"], "imports")
        self.assertEqual(by_file["c.py"], "dynamic_nonconstant")
        for f in ("d.py", "e.py", "f.py"):
            self.assertEqual(by_file[f], "dynamic_code", f)

    def test_scanner_flags_each_known_form_of_dependency_on_kb(self):
        """Static import, from-import, dynamic import, __import__, subprocess by name, and python -m kb."""
        bad = {
            "a.py": "import kb\n", "b.py": "from kb.cli import main\n",
            "c.py": "import importlib\nimportlib.import_module('kb.search')\n", "d.py": "__import__('kb')\n",
            "e.py": "import subprocess\nsubprocess.run(['kb', 'search', 'x'])\n",
            "f.py": "import subprocess, sys\nsubprocess.run([sys.executable, '-m', 'kb'])\n",
            "g.py": "import os\nos.system('kb status')\n", "h.py": "import requests\n",
        }
        problems = importgraph.scan_harness(write_tree(self, bad))
        by_file = {p[0]: p[1] for p in problems}
        for f, kind in {"a.py": "imports", "b.py": "imports", "c.py": "imports", "d.py": "imports", "e.py": "runs_process",
                        "f.py": "runs_process", "g.py": "runs_process", "h.py": "non_stdlib_import"}.items():
            self.assertEqual(by_file.get(f), kind, f)

    def test_scanner_passes_a_clean_tree_and_ignores_mentions(self):
        """A comment or string that merely says 'kb' must not trigger it (the flaw of text search)."""
        clean = {"ok.py": "import json, os\nimport ok2\n# kb is optional and not imported here\nNAME = 'knowledge base'\n",
                 "ok2.py": "import subprocess\nsubprocess.run(['git', 'status'])\n"}
        self.assertEqual(importgraph.scan_harness(write_tree(self, clean)), [])

    @unittest.skipUnless(os.path.isdir(HARNESS_DIR), "harness source not found (set KB_HARNESS_DIR)")
    def test_real_harness_has_no_kb_dependency(self):
        """O1 on the real code: the harness imports only the standard library and its own folder, and never runs kb."""
        self.assertEqual(importgraph.scan_harness(HARNESS_DIR), [])


if __name__ == "__main__":
    unittest.main()
