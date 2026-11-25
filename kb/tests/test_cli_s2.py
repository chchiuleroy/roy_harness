"""S2f: the review commands end to end through the command line (patch, review list/show/approve/reject/resolve/purge)."""
import io
import os
import re
import unittest
from contextlib import redirect_stderr

from kb import cli

from .support import tmpdir


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir(self)
        self.home = os.path.join(self.d, "h")
        self.src = os.path.join(self.d, "src")
        os.makedirs(self.src)
        self.doc = os.path.join(self.src, "doc.md")
        self.write("# Notes\n\nFirst paragraph about falcons.\n\nSecond paragraph has a typpo in it.\n")

    def write(self, text):
        with open(self.doc, "w", encoding="utf-8") as f:
            f.write(text)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(["--home", self.home, *argv], out=out)
        return code, out.getvalue(), err.getvalue()

    def start(self):
        self.run_cli("init")
        self.run_cli("add", self.src, "--settle", "0")
        self.run_cli("commit")
        out = self.run_cli("show", "doc.md")[1]
        return re.search(r"(uv_\w+)\s+'Second paragraph", out).group(1)


class TestPatchFlow(Cli):
    def test_a_patch_waits_for_approval_then_shows_after_the_next_commit(self):
        uv = self.start()
        code, out, _ = self.run_cli("patch", uv, "--find", "typpo", "--replace", "typo", "--by", "roy")
        self.assertEqual(code, 0)
        self.assertIn("nothing changes until you approve", out)
        self.assertIn("'Second paragraph has a typo in it.'", out)                    # the new text is shown before the user decides
        pid = re.search(r"approve (pr_\w+)", out).group(1)
        self.assertIn("pending proposals: 1", self.run_cli("review", "list")[1])
        self.assertEqual(self.run_cli("review", "approve", pid, "--by", "roy")[0], 0)
        self.assertIn("no results", self.run_cli("search", "typo")[1])               # applied, not committed yet
        self.run_cli("commit")
        self.assertIn("typo", self.run_cli("search", "typo")[1])
        self.assertIn("no results", self.run_cli("search", "typpo")[1])

    def test_yes_does_it_in_one_step_and_a_reject_drops_it(self):
        uv = self.start()
        self.assertEqual(self.run_cli("patch", uv, "--set-text", "Second paragraph is fine.", "--by", "roy", "--yes")[0], 0)
        out = self.run_cli("patch", uv, "--find", "fine", "--replace", "great", "--by", "roy")[1]
        pid = re.search(r"approve (pr_\w+)", out).group(1)
        self.assertEqual(self.run_cli("review", "reject", pid, "--by", "roy")[0], 0)
        code, _, err = self.run_cli("review", "approve", pid, "--by", "roy")
        self.assertEqual(code, 2)
        self.assertIn("rejected", err)

    def test_the_command_refuses_a_patch_with_nothing_to_do(self):
        uv = self.start()
        self.assertEqual(self.run_cli("patch", uv, "--by", "roy")[0], 2)
        code, _, err = self.run_cli("patch", uv, "--find", "nowhere", "--replace", "x", "--by", "roy")
        self.assertEqual(code, 2)
        self.assertIn("exactly once", err)


class TestConflictFlow(Cli):
    def test_a_changed_paragraph_goes_to_the_queue_and_resolve_drops_it(self):
        uv = self.start()
        self.run_cli("patch", uv, "--find", "typpo", "--replace", "typo", "--by", "roy", "--yes")
        self.run_cli("commit")
        self.write("# Notes\n\nFirst paragraph about falcons.\n\nEntirely different sentences about harbours and tides now.\n")
        self.run_cli("add", self.src, "--settle", "0")
        out = self.run_cli("process")[1]
        self.assertIn("1 need a decision", out)
        listing = self.run_cli("review", "list")[1]
        self.assertIn("patches that need a decision: 1", listing)
        pat = re.search(r"(pat_\w+)", listing).group(1)
        code, out, _ = self.run_cli("review", "resolve", pat, "--by", "roy")
        self.assertEqual(code, 0)
        self.assertIn("dropped", out)
        self.assertIn("patches that need a decision: 0", self.run_cli("review", "list")[1])

    def test_a_paragraph_that_only_moved_is_followed_without_asking(self):
        uv = self.start()
        self.run_cli("patch", uv, "--find", "typpo", "--replace", "typo", "--by", "roy", "--yes")
        self.run_cli("commit")
        self.write("# Notes\n\nA new first paragraph.\n\nFirst paragraph about falcons.\n\nSecond paragraph has a typpo in it.\n")
        self.run_cli("add", self.src, "--settle", "0")
        out = self.run_cli("commit")[1]
        self.assertIn("1 followed their unit", out)
        self.assertIn("typo", self.run_cli("search", "typo")[1])
        self.assertIn("no results", self.run_cli("search", "typpo")[1])


class TestPurge(Cli):
    def test_purge_blanks_old_decided_differences(self):
        uv = self.start()
        self.run_cli("patch", uv, "--find", "typpo", "--replace", "typo", "--by", "roy", "--yes")
        code, out, _ = self.run_cli("review", "purge", "--days", "0")
        self.assertEqual(code, 0)
        self.assertIn("blanked the difference of 1 decided proposal", out)


if __name__ == "__main__":
    unittest.main()
