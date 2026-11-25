"""S4, R1 (dependency direction): kb must not know the harness or superagent. The harness is only ever STARTED, as a child process, by a path that the person
gives (ask_runner); no module of kb imports it, in any of the ways a program can import: `import x`, `from x import y`, `importlib.import_module("x")`,
`__import__("x")`. Gate 2 asks for a syntax-tree scan, not a text search; the scan is tested on samples of each form so that it cannot go blind."""
import ast
import os
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "kb")
FORBIDDEN = {"harness", "superagent", "harness_vllm", "harness_hooks", "harness_mcp"}


def imported_names(source):
    """Every top-level module name that `source` imports, including names given as string constants to importlib or __import__."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            fn = node.func
            called = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if called in ("import_module", "__import__") and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                names.add(node.args[0].value.split(".")[0])
    return names


class TestScanner(unittest.TestCase):
    def test_every_form_of_import_is_seen(self):
        for source in ("import harness", "import harness.sub as h", "from harness import tools", "from harness.sub import x",
                       "import importlib\nimportlib.import_module('harness.sub')", "__import__('harness')", "import os, superagent"):
            with self.subTest(source):
                self.assertTrue(imported_names(source) & FORBIDDEN, source)

    def test_a_relative_import_is_not_a_foreign_module_and_a_mention_in_a_string_is_not_an_import(self):
        self.assertEqual(imported_names("from . import ask_frame\nfrom .errors import KbError"), set())
        self.assertEqual(imported_names("path = 'roy_harness/src/harness/harness_vllm.py'\nprint('harness')"), set())


class TestKbDoesNotKnowTheHarness(unittest.TestCase):
    def test_no_module_of_kb_imports_the_harness_or_superagent(self):
        offenders = {}
        for name in sorted(os.listdir(SRC)):
            if name.endswith(".py"):
                with open(os.path.join(SRC, name), encoding="utf-8") as f:
                    bad = imported_names(f.read()) & FORBIDDEN
                if bad:
                    offenders[name] = sorted(bad)
        self.assertEqual(offenders, {})
        self.assertTrue(os.path.isfile(os.path.join(SRC, "ask_runner.py")))                  # the scan looked at the real tree


if __name__ == "__main__":
    unittest.main()
