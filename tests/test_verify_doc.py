"""docs/VERIFY.md and AGENTS.md must keep matching verify.py.

Why it matters: someone who follows the document gets a "verified" they can trust only if the document names the
same live switches, the same stage options and the same files as the program. These tests compare names and paths,
not sentences, so rewording the text does not turn them red."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import verify  # noqa: E402


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestVerifyDoc(unittest.TestCase):
    def test_every_live_switch_is_named_in_the_doc(self):
        """A switch the doc does not mention is one a reader will leave set in their shell and then wonder why the
        'offline' check touched a real server (or why it was slow)."""
        doc = read("docs/VERIFY.md")
        for name in verify.LIVE_SWITCHES:
            self.assertIn(name, doc, f"docs/VERIFY.md does not name {name}")

    def test_every_stage_name_is_documented_as_an_only_option(self):
        doc = read("docs/VERIFY.md")
        for stage in verify.default_stages():
            self.assertIn(f"--only {stage['name']}", doc, f"docs/VERIFY.md has no `--only {stage['name']}` example")

    def test_every_optional_package_is_named_in_the_doc(self):
        """kb needs these two; the doc must say a missing one is a failure, and under which import name."""
        doc = read("docs/VERIFY.md")
        for mod in verify.KB_OPTIONAL:
            self.assertIn(mod, doc, f"docs/VERIFY.md does not name {mod}")

    def test_the_agent_file_points_at_a_document_that_exists(self):
        """AGENTS.md is what a coding agent reads first; a dead link there means the rules are never read."""
        text = read("AGENTS.md")
        self.assertIn("docs/VERIFY.md", text)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "docs", "VERIFY.md")))
        self.assertIn("python verify.py", text)

    def test_the_skill_pointer_points_at_the_doc_and_the_entry(self):
        """The skill is only a pointer; the rules live in docs/VERIFY.md. If the relative link breaks, an agent that
        loads the skill never reaches the rules, and one that reaches them never learns the command."""
        skill_dir = os.path.join(ROOT, ".claude", "skills", "verify")
        text = read(".claude/skills/verify/SKILL.md")
        self.assertIn("python verify.py", text)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "verify.py")))
        self.assertIn("../../../docs/VERIFY.md", text)
        self.assertTrue(os.path.isfile(os.path.normpath(os.path.join(skill_dir, "../../../docs/VERIFY.md"))))

    def test_the_doc_names_the_ci_jobs_that_exist(self):
        """The required-check names in the doc are the job ids in the workflow; a rename must not go unnoticed."""
        workflow = read(".github/workflows/ci.yml")
        doc = read("docs/VERIFY.md")
        for job in ("offline-tests", "kb-tests"):
            self.assertIn(f"  {job}:", workflow)
            self.assertIn(job, doc)


if __name__ == "__main__":
    unittest.main()
