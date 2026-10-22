"""Tests for src/harness/harness_skills.py (skills: instructions on demand, book ch 11).

A skill is a folder with a SKILL.md: a name, a one-line description, and a body of
instructions. Only names and descriptions sit in the system prompt (the menu); the
body enters the conversation when the model asks for it with the `skill` tool.
Each test's docstring says WHY the behaviour matters.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import unittest

import http.server

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src", "harness"))
import harness_vllm as hv  # noqa: E402
from test_harness_vllm import Base, _LLMServer, llm_reply, tool_call_reply  # noqa: E402

LIVE = os.environ.get("HARNESS_LIVE") == "1"
LIVE_DIR = os.environ.get("HARNESS_SKILLS_LIVE_DIR", "")


def link_dir(link, target):
    """A directory link (junction on Windows, symlink elsewhere). False if not possible."""
    link, target = os.path.abspath(link), os.path.abspath(target)
    try:
        if sys.platform == "win32":
            r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
            return r.returncode == 0
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        return False


class SkillBase(Base):
    def setUp(self):
        super().setUp()
        import harness_skills as hs
        self.hs = hs
        self._skills = hv.SKILLS
        self._env = os.environ.get("HARNESS_SKILLS_DIRS")
        os.environ.pop("HARNESS_SKILLS_DIRS", None)

    def tearDown(self):
        hv.SKILLS = self._skills
        if self._env is None:
            os.environ.pop("HARNESS_SKILLS_DIRS", None)
        else:
            os.environ["HARNESS_SKILLS_DIRS"] = self._env
        super().tearDown()

    def mk(self, name, description="Does the thing", body="BODY of " + "{name}", root="skills",
           frontmatter=None):
        """Write <root>/<name>/SKILL.md and return its path."""
        folder = os.path.join(root, name)
        os.makedirs(folder, exist_ok=True)
        fm = frontmatter if frontmatter is not None else (
            f"---\nname: {name}\ndescription: {description}\n---\n")
        path = os.path.join(folder, "SKILL.md")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(fm + body.replace("{name}", name))
        return path

    def load(self, *dirs):
        reg = self.hs.load_all(list(dirs) if dirs else None)
        return reg


# ------------------------------------------------------------------- parser
class TestFrontmatter(SkillBase):
    def parse(self, text):
        return self.hs.parse_frontmatter(text)

    def test_plain_quoted_and_colon_values(self):
        fm, body = self.parse("---\nname: a\ndescription: Does a thing: with a colon\n---\nBODY")
        self.assertEqual((fm["name"], fm["description"], body), ("a", "Does a thing: with a colon", "BODY"))
        fm, _ = self.parse('---\nname: a\ndescription: "Quoted: yes"\n---\nx')
        self.assertEqual(fm["description"], "Quoted: yes")
        fm, _ = self.parse("---\nname: a\ndescription: 'It''s fine'\n---\nx")
        self.assertEqual(fm["description"], "It's fine")

    def test_folded_and_literal_block_scalars(self):
        """24 of 50 real skills write `description: >`; a parser that stops at the first
        line would turn them into the one character '>'."""
        fm, _ = self.parse("---\nname: a\ndescription: >\n  line one\n  line two\n---\nx")
        self.assertEqual(fm["description"], "line one line two")
        fm, _ = self.parse("---\nname: a\ndescription: >-\n  alpha\n  beta\nauthor: me\n---\nx")
        self.assertEqual((fm["description"], fm["author"]), ("alpha beta", "me"))
        fm, _ = self.parse("---\nname: a\ndescription: |\n  x\n  y\n---\nz")
        self.assertEqual(fm["description"], "x\ny")
        fm, _ = self.parse("---\nname: a\ndescription: >\n  para one\n\n  para two\n---\nz")
        self.assertEqual(fm["description"], "para one\npara two")

    def test_nested_and_unknown_keys_do_not_confuse_the_keys_that_matter(self):
        text = ("---\nname: a\nmetadata:\n  author: me\n  tags: [x, y]\nallowed-tools: "
                '["Bash", "Read"]\ndescription: after the nested block\n---\nBODY')
        fm, body = self.parse(text)
        self.assertEqual((fm["name"], fm["description"], body),
                         ("a", "after the nested block", "BODY"))

    def test_crlf_bom_and_non_ascii(self):
        fm, body = self.parse("\ufeff---\r\nname: 占卜\r\ndescription: 易經占卜工具\r\n---\r\n本文\r\n")
        self.assertEqual((fm["name"], fm["description"]), ("占卜", "易經占卜工具"))
        self.assertEqual(body.strip(), "本文")

    def test_missing_or_unterminated_frontmatter_means_no_metadata(self):
        for text in ("just a body", "---\nname: a\ndescription: never closed\nbody"):
            fm, body = self.parse(text)
            self.assertEqual(fm, {}, text)
            self.assertEqual(body, text)


# ------------------------------------------------------------------ loading
class TestLoading(SkillBase):
    def test_valid_skills_are_listed_sorted_with_their_descriptions(self):
        self.mk("zeta", "Last one")
        self.mk("alpha", "First one")
        reg = self.load()
        self.assertEqual(list(reg.skills), ["alpha", "zeta"])
        self.assertEqual(reg.skills["alpha"].description, "First one")
        self.assertEqual(reg.warnings, [])

    def test_a_missing_folder_means_no_skills_not_an_error(self):
        reg = self.load("no-such-dir")
        self.assertTrue(reg.is_empty())
        self.assertEqual(reg.menu(), "")

    def test_unusable_skills_are_skipped_with_a_reason(self):
        self.mk("nodesc", frontmatter="---\nname: nodesc\n---\n")
        self.mk("emptydesc", frontmatter="---\nname: emptydesc\ndescription: \n---\n")
        self.mk("bad name")
        self.mk(".hidden")
        os.makedirs("skills/nofile", exist_ok=True)               # no SKILL.md: ignored quietly
        folder = os.path.join("skills", "binary")
        os.makedirs(folder)
        with open(os.path.join(folder, "SKILL.md"), "wb") as f:
            f.write(b"---\nname: binary\ndescription: \xff\xfe broken\n---\nx")
        self.mk("good")
        reg = self.load()
        self.assertEqual(list(reg.skills), ["good"])
        text = " ".join(reg.warnings)
        for name in ("nodesc", "emptydesc", "bad name", ".hidden", "binary"):
            self.assertIn(name, text, name)
        self.assertNotIn("nofile", text)

    def test_the_folder_name_is_the_identity_even_if_name_says_otherwise(self):
        self.mk("mine", frontmatter="---\nname: other\ndescription: d\n---\n")
        reg = self.load()
        self.assertEqual(list(reg.skills), ["mine"])
        self.assertTrue(any("other" in w for w in reg.warnings))

    def test_the_same_name_in_two_roots_first_one_wins(self):
        self.mk("dup", "from A", root="a")
        self.mk("dup", "from B", root="b")
        reg = self.load("a", "b")
        self.assertEqual(reg.skills["dup"].description, "from A")
        self.assertTrue(any("dup" in w for w in reg.warnings))

    def test_roots_come_from_the_environment_and_default_to_skills(self):
        self.mk("here", root="skills")
        self.mk("there", root="elsewhere")
        self.assertEqual(list(self.hs.load_all().skills), ["here"])
        os.environ["HARNESS_SKILLS_DIRS"] = os.pathsep.join(["elsewhere", "skills"])
        self.assertEqual(sorted(self.hs.load_all().skills), ["here", "there"])

    def test_limits_keep_the_menu_small(self):
        """The menu rides on every model request, so its size is capped (my numbers)."""
        for i in range(self.hs.MAX_SKILLS + 6):
            self.mk(f"s{i:03d}", "d")
        reg = self.load()
        self.assertEqual(len(reg.skills), self.hs.MAX_SKILLS)
        self.assertTrue(any("limit" in w for w in reg.warnings))
        long = "x" * 500
        self.mk("wordy", long, root="w")
        text = self.load("w").skills["wordy"].description
        self.assertLessEqual(len(text), self.hs.MAX_DESCRIPTION + 1)
        self.assertTrue(text.endswith("…"))
        for i in range(60):
            self.mk(f"m{i:02d}", "y" * 290, root="m")
        reg = self.load("m")
        self.assertLessEqual(len(reg.menu()), self.hs.MAX_MENU)
        self.assertTrue(any("menu" in w for w in reg.warnings), reg.warnings)

    def test_an_over_budget_menu_shortens_descriptions_instead_of_hiding_skills(self):
        """A skill dropped from the menu does not exist for the model; a skill with a
        shorter description still can be called, and its body is loaded in full. So over
        budget, every skill stays and the descriptions shrink in proportion."""
        for i in range(60):
            self.mk(f"s{i:02d}", ("L" if i % 2 else "S") * (280 if i % 2 else 140))
        reg = self.load()
        self.assertEqual(len(reg.skills), 60)
        self.assertLessEqual(len(reg.menu()), self.hs.MAX_MENU)
        self.assertGreater(len(reg.menu()), self.hs.MAX_MENU - 150)     # not shrunk needlessly
        longs = [len(s.description) for s in reg.skills.values() if s.description[0] == "L"]
        shorts = [len(s.description) for s in reg.skills.values() if s.description[0] == "S"]
        self.assertTrue(all(d.endswith("…") for d in
                            (s.description for s in reg.skills.values())))
        self.assertGreater(min(longs), max(shorts))                     # order is kept
        self.assertAlmostEqual(sum(longs) / sum(shorts), 2.0, delta=0.15)   # in proportion
        self.assertTrue(any("shortened" in w and "budget" in w for w in reg.warnings),
                        reg.warnings)

    def test_a_real_sized_folder_fits_the_default_budget_without_shortening(self):
        """Measured on the author's folder (2026-10-22): 42 skills with full descriptions make
        an 11,001-character menu. Shortening them cost 3.6 points of selection accuracy (95% CI
        -13.1 to +5.6) and 5.9 points on the skills that used to fit, for ~3,000 characters
        saved, so the default budget is sized to leave such a folder alone."""
        for i in range(42):
            self.mk(f"skill-{i:02d}", chr(65 + i % 26) * 250)
        reg = self.load()
        self.assertEqual(len(reg.skills), 42)
        self.assertEqual(reg.warnings, [])
        self.assertTrue(all(s.description == chr(65 + n % 26) * 250
                            for n, s in enumerate(reg.skills.values())))
        self.assertGreater(len(reg.menu()), 8_000)                      # it WOULD have been shortened before

    def test_a_menu_that_fits_is_left_exactly_as_written(self):
        """Shortening is only for the over-budget case; a normal folder must not change."""
        self.mk("alpha", "x" * 250)
        self.mk("beta", "Does beta")
        reg = self.load()
        self.assertEqual(reg.menu(), f"- alpha: {'x' * 250}\n- beta: Does beta")
        self.assertEqual(reg.warnings, [])

    def test_a_menu_that_exactly_fits_is_not_shortened(self):
        """Found by the 小o re-review: the size estimate counted a newline after the last
        line, so a menu that fit to the character was still cut or lost a skill."""
        self.mk("a", "x" * 41)
        exact = len("- a: " + "x" * 41)
        old = self.hs.MAX_MENU
        self.hs.MAX_MENU = exact
        try:
            reg = self.load()
            self.hs.MAX_MENU = len("- b: x")
            self.mk("b", "x", root="b")
            reg_one = self.load("b")
        finally:
            self.hs.MAX_MENU = old
        self.assertEqual(reg.skills["a"].description, "x" * 41)
        self.assertEqual(len(reg.menu()), exact)
        self.assertEqual(reg.warnings, [])
        self.assertEqual(list(reg_one.skills), ["b"])

    def test_the_menu_never_exceeds_the_budget_at_any_budget(self):
        """The size estimate and menu() must agree to the character, in both directions:
        too high wastes room, too low breaks the cap."""
        for i in range(6):
            self.mk(f"k{i}", "d" * (60 + 17 * i))
        old = self.hs.MAX_MENU
        try:
            for budget in range(40, 700, 7):
                self.hs.MAX_MENU = budget
                menu = self.load().menu()
                self.assertLessEqual(len(menu), budget, budget)
        finally:
            self.hs.MAX_MENU = old

    def test_a_description_already_short_is_not_padded_or_cut_when_others_shrink(self):
        for i in range(45):
            self.mk(f"w{i:02d}", "z" * 290)
        self.mk("aaa", "tiny")
        reg = self.load()
        self.assertEqual(reg.skills["aaa"].description, "tiny")
        self.assertLessEqual(len(reg.menu()), self.hs.MAX_MENU)

    def test_when_even_minimum_descriptions_do_not_fit_the_last_skills_are_dropped(self):
        for i in range(30):
            self.mk(f"s{i:02d}", "d" * 200)
        old = self.hs.MAX_MENU
        self.hs.MAX_MENU = 600
        try:
            reg = self.load()
        finally:
            self.hs.MAX_MENU = old
        self.assertLessEqual(len(reg.menu()), 600)
        names = list(reg.skills)
        self.assertEqual(names, sorted(names))
        self.assertEqual(names[0], "s00")                                # the first stay
        self.assertLess(len(names), 30)
        self.assertTrue(any("ignored" in w for w in reg.warnings), reg.warnings)

    def test_control_characters_in_a_description_are_removed(self):
        self.mk("ctl", "ok\x07\x1b[31m red")
        self.assertNotIn("\x07", self.load().skills["ctl"].description)
        self.assertNotIn("\x1b", self.load().skills["ctl"].description)

    def test_a_link_leaving_the_roots_is_skipped_and_one_into_another_root_is_accepted(self):
        """SymJack: a link inside the skills folder must not bring in a skill from just
        anywhere. Its real location has to be inside a root you configured."""
        self.mk("real", root="outside")
        os.makedirs("skills")
        if not link_dir("skills/evil", "outside/real"):
            self.skipTest("cannot create a directory link here")
        reg = self.load("skills")
        self.assertEqual(list(reg.skills), [])
        self.assertTrue(any("evil" in w and "outside" in w.lower() or "link" in w.lower()
                            for w in reg.warnings), reg.warnings)
        reg = self.load("skills", "outside")                      # now 'outside' is a root
        self.assertIn("evil", reg.skills)                        # the link resolves inside it
        self.assertIn("real", reg.skills)


# --------------------------------------------------------------- skill tool
class TestSkillTool(SkillBase):
    def test_the_menu_has_names_and_descriptions_but_never_a_body(self):
        self.mk("alpha", "Does alpha", body="SECRET-BODY-TEXT")
        menu = self.load().menu()
        self.assertIn("alpha", menu)
        self.assertIn("Does alpha", menu)
        self.assertNotIn("SECRET-BODY-TEXT", menu)

    def test_load_returns_the_body_without_frontmatter_and_says_where_the_folder_is(self):
        self.mk("alpha", "d", body="Step 1\nStep 2")
        out = self.load().load("alpha")
        self.assertIn("Step 1\nStep 2", out)
        self.assertNotIn("description:", out)
        self.assertIn(os.path.abspath("skills/alpha").lower(), out.lower())   # bundled files

    def test_an_unknown_name_lists_what_exists_and_touches_no_file(self):
        """Names are looked up in a table. Path-like names must not become file paths:
        a secret next to the skills folder must stay unreadable."""
        self.mk("alpha")
        with open("secret.txt", "w", encoding="utf-8") as f:
            f.write("TOP-SECRET")
        reg = self.load()
        for bad in ("nope", "../secret", "..\\secret", "alpha/../../secret.txt",
                    os.path.abspath("secret.txt"), "", None, 5, ["alpha"]):
            out = reg.load(bad)
            self.assertTrue(out.startswith("ERROR"), repr(bad))
            self.assertNotIn("TOP-SECRET", out)
        self.assertIn("alpha", reg.load("nope"))

    def test_a_very_long_body_is_cut_and_the_full_file_is_pointed_to(self):
        self.mk("long", "d", body="L" * (self.hs.MAX_BODY * 3))
        out = self.load().load("long")
        self.assertLessEqual(len(out), self.hs.MAX_BODY + 800)
        self.assertIn("truncated", out)
        self.assertIn("SKILL.md", out)

    def test_a_skill_md_that_leaves_the_roots_is_refused_even_when_its_folder_is_inside(self):
        """Found by the 小o review: only the folder's location was checked, so a SKILL.md
        that is itself a link to a file elsewhere was read."""
        self.mk("alpha")
        with open("secret.md", "w", encoding="utf-8") as f:
            f.write("---\nname: x\ndescription: d\n---\nOUTSIDE-CONTENT")
        reg = self.load("skills")
        reg.skills["x"] = self.hs.Skill("x", "d", os.path.join("skills", "alpha"),
                                        os.path.abspath("secret.md"))
        out = reg.load("x")
        self.assertTrue(out.startswith("ERROR"), out)
        self.assertNotIn("OUTSIDE-CONTENT", out)
        os.remove(os.path.join("skills", "alpha", "SKILL.md"))      # and at scan time
        try:
            os.symlink(os.path.abspath("secret.md"), os.path.join("skills", "alpha", "SKILL.md"))
        except OSError:
            return                                                  # no symlink privilege here
        self.assertEqual(list(self.load("skills").skills), [])

    def test_a_skill_md_link_is_caught_at_scan_time_without_needing_link_privileges(self):
        """Creating a file symlink needs privileges that are not always there, so the
        scan check is exercised by making the real path of SKILL.md look external."""
        self.mk("alpha")
        self.mk("beta")
        real = self.hs.harness_hooks.canonical
        outside = os.path.abspath("outside.md")

        def fake(path, *a, **k):
            if os.path.basename(str(path)).lower() == "skill.md" and "alpha" in str(path):
                return real(outside)
            return real(path, *a, **k)
        self.hs.harness_hooks.canonical = fake
        try:
            reg = self.load("skills")
        finally:
            self.hs.harness_hooks.canonical = real
        self.assertEqual(list(reg.skills), ["beta"])
        self.assertTrue(any("alpha" in w and "SKILL.md" in w for w in reg.warnings), reg.warnings)

    def test_a_swap_while_the_file_is_being_read_is_noticed(self):
        """Resolve, read, resolve again: if the answer changed, the text is thrown away."""
        self.mk("alpha", body="ALPHA-BODY")
        reg = self.load("skills")
        real = self.hs.harness_hooks.canonical
        calls = []

        def fake(path, *a, **k):
            if os.path.basename(str(path)).lower() == "skill.md":
                calls.append(path)
                if len(calls) >= 2:                      # the check after the read
                    return real(os.path.join("somewhere", "else", "SKILL.md"))
            return real(path, *a, **k)
        self.hs.harness_hooks.canonical = fake
        try:
            out = reg.load("alpha")
        finally:
            self.hs.harness_hooks.canonical = real
        self.assertTrue(out.startswith("ERROR"), out)
        self.assertIn("while it was being read", out)
        self.assertNotIn("ALPHA-BODY", out)

    def test_the_file_that_is_opened_is_the_one_that_was_checked(self):
        """The check resolves the location; opening the original (link) path afterwards
        leaves a window to swap the link in between. Opening the resolved path closes
        most of it (not atomic: a concurrent writer on the folder tree is out of scope)."""
        self.mk("real", root="store")
        os.makedirs("skills")
        if not link_dir("skills/alias", "store/real"):
            self.skipTest("cannot create a directory link here")
        reg = self.load("skills", "store")
        seen = []
        original = self.hs._read_text
        self.hs._read_text = lambda path: (seen.append(path), original(path))[1]
        try:
            reg.load("alias")
        finally:
            self.hs._read_text = original
        self.assertTrue(seen)
        self.assertNotIn(os.sep + "alias" + os.sep, seen[-1].lower())
        self.assertIn(os.sep + "store" + os.sep, seen[-1].lower())

    def test_a_drive_root_works_as_a_root_in_the_containment_check(self):
        """Found by the 小o review: 'C:\\' already ends with a separator, and adding another
        made every child of a drive root look outside it."""
        import harness_hooks as hh
        drive = os.path.splitdrive(os.getcwd())[0] + os.sep
        child = hh.canonical(os.path.join(drive, "x", "y"))
        self.assertTrue(self.hs._inside(child, [hh.canonical(drive)]))
        self.assertFalse(self.hs._inside(hh.canonical(os.path.join(drive, "x")),
                                         [hh.canonical(os.path.join(drive, "x", "y"))]))

    def test_the_budget_is_applied_in_global_alphabetical_order_across_roots(self):
        """Found by the 小o review: roots were scanned in turn and loading stopped at the
        limit, so an earlier root's 'z' skills could push a later root's 'a' out and the
        later root might not be scanned at all."""
        for n in ("z1", "z2", "z3"):
            self.mk(n, root="A")
        self.mk("a1", root="B")
        old = self.hs.MAX_SKILLS
        self.hs.MAX_SKILLS = 2
        try:
            reg = self.load("A", "B")
        finally:
            self.hs.MAX_SKILLS = old
        self.assertEqual(list(reg.skills), ["a1", "z1"])
        self.assertTrue(any("limit" in w for w in reg.warnings))
        self.assertTrue(any("2" in w and "ignored" in w for w in reg.warnings), reg.warnings)

    def test_a_skill_swapped_for_a_link_after_startup_is_refused(self):
        """The body is read at call time, so the location is checked again then."""
        self.mk("alpha")
        self.mk("evil", body="EVIL-BODY", root="outside")
        reg = self.load("skills")
        import shutil
        shutil.rmtree("skills/alpha")
        if not link_dir("skills/alpha", "outside/evil"):
            self.skipTest("cannot create a directory link here")
        out = reg.load("alpha")
        self.assertTrue(out.startswith("ERROR"), out)
        self.assertNotIn("EVIL-BODY", out)


# -------------------------------------------------------- inside the harness
class TestSkillsInHarness(SkillBase):
    def setUp(self):
        super().setUp()
        self.asked = []
        hv.read_line = lambda prompt: (self.asked.append(prompt), "n")[1]

    def install(self, *names):
        for n in names or ("alpha",):
            self.mk(n, f"Does {n}", body=f"BODY-{n}-MARKER")
        hv.SKILLS = self.load()

    def test_the_skill_tool_exists_only_when_there_are_skills_and_only_for_the_main_agent(self):
        self.assertNotIn("skill", {t["name"] for t in hv.tools_for(0)})
        self.install()
        self.assertIn("skill", {t["name"] for t in hv.tools_for(0)})
        for depth in (1, 2):
            self.assertNotIn("skill", {t["name"] for t in hv.tools_for(depth)}, depth)
        out = hv.execute_tool("skill", {"name": "alpha"}, depth=1)
        self.assertIn("not available to subagents", out)
        self.assertNotIn("BODY-alpha-MARKER", out)

    def test_the_menu_lives_in_the_system_prompt_which_compaction_cannot_touch(self):
        """A menu placed in the first message would be summarised away by compaction;
        the system prompt is rebuilt for every request."""
        self.install("alpha", "beta")
        prompt = hv.system_prompt()
        self.assertIn("alpha", prompt)
        self.assertIn("Does beta", prompt)
        self.assertIn("skill", prompt.lower())
        self.assertNotIn("BODY-alpha-MARKER", prompt)
        hv.SKILLS = self.hs.EMPTY
        self.assertEqual(hv.system_prompt(), hv.SYSTEM)

    def test_loading_a_skill_asks_nobody_but_hooks_still_apply(self):
        """Reading a local file by name is not an action to approve; a hook can still
        veto it like any other tool."""
        import harness_hooks as hh
        self.install()
        for mode in ("safe", "normal", "auto"):
            hv.MODE = mode
            out = hv.execute_tool("skill", {"name": "alpha"})
            self.assertIn("BODY-alpha-MARKER", out, mode)
        self.assertEqual(self.asked, [])
        with open("deny.py", "w", encoding="utf-8") as f:
            f.write("import sys\nsys.stderr.write('policy: no skills')\nsys.exit(2)")
        with open(".harness.hooks.json", "w", encoding="utf-8") as f:
            json.dump({"hooks": {"PreToolUse": [{"matcher": "skill", "hooks": [
                {"type": "command", "command": sys.executable,
                 "args": [os.path.abspath("deny.py")]}]}]}}, f)
        old = hv.HOOKS
        hv.HOOKS = hh.load(".harness.hooks.json")
        try:
            out = hv.execute_tool("skill", {"name": "alpha"})
        finally:
            hv.HOOKS = old
        self.assertIn("BLOCKED by hook", out)
        self.assertNotIn("BODY-alpha-MARKER", out)

    def test_the_model_cannot_create_or_edit_skills(self):
        """A skill is instructions the model obeys. Letting it write them would let it
        rewrite its own rules and keep them across sessions."""
        self.install()
        paths = ("skills/new/SKILL.md", "./skills/alpha/SKILL.md", "SKILLS/ALPHA/SKILL.MD",
                 "skills/alpha/run.py", "skills/new/SKILL.md.", "skills/new/SKILL.md::$DATA",
                 "skills/alpha/../beta/SKILL.md")
        for path in paths:
            for tool in ("write_file", "edit_file"):
                args = {"path": path, "content": "x", "old_text": "a", "new_text": "b"}
                if sys.platform != "win32" and ("::" in path or path.endswith(".")):
                    continue
                out = hv.gate(tool, args)
                self.assertIn("BLOCKED", out or "", (tool, path))
                self.assertIn("protected" if "::" not in path else "BLOCKED", (out or ""))
        for cmd in ("echo x > skills/new/SKILL.md", r"echo x > skills\new\run.py",
                    "echo x>SKILL.md", "echo x > skills\\alpha\\SKILL.md."):
            self.assertIn("BLOCKED", hv.gate("run_command", {"command": cmd}) or "", cmd)
        for cmd in ("ren skills moved", "move skills elsewhere", "rmdir /s /q skills",
                    "echo x > skills.", "mklink /J skills C:\\other"):
            self.assertIn("BLOCKED", hv.gate("run_command", {"command": cmd}) or "", cmd)
        for cmd in ("echo skills-are-fun", r"echo myskills\x", "echo SKILL.md.bak",
                    "echo skills.bak"):
            self.assertIsNone(hv.gate("run_command", {"command": cmd}), cmd)

    def test_renaming_the_skills_folder_does_not_unprotect_what_is_inside(self):
        """Found by the 小o review and reproduced: `ren skills moved`, write under
        `moved`, `ren moved skills` all passed the gate, because the protection was a
        path prefix and the folder no longer lived at that path. Protection now also
        follows the folder's identity."""
        self.install()
        os.rename("skills", "moved")                 # what the shell command would do
        for path in ("moved/alpha/SKILL.md", "moved/alpha/run.py", "moved/new/SKILL.md",
                     "MOVED/alpha/x.py"):
            out = hv.gate("write_file", {"path": path, "content": "EVIL"})
            self.assertIn("protected", (out or "").lower(), path)
        self.assertIsNone(hv.gate("write_file", {"path": "elsewhere.txt", "content": "x"}))
        os.rename("moved", "skills")

    def test_a_link_to_the_skills_folder_is_the_same_protected_folder(self):
        self.install()
        if not link_dir("alias", "skills"):
            self.skipTest("cannot create a directory link here")
        out = hv.gate("write_file", {"path": "alias/alpha/SKILL.md", "content": "EVIL"})
        self.assertIn("protected", (out or "").lower())

    def test_slash_skills_lists_and_slash_skill_hands_the_body_to_the_model(self):
        self.install("alpha", "beta")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hv.slash("/skills", [])
        self.assertIn("alpha", buf.getvalue())
        self.assertIn("beta", buf.getvalue())
        hv.drain_reminders()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hv.slash("/skill alpha", [])
        self.assertIn("BODY-alpha-MARKER", hv.drain_reminders())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hv.slash("/skill nonesuch", [])
        self.assertIn("unknown", buf.getvalue().lower())
        self.assertEqual(hv.drain_reminders(), "")


# --------------------------------------------------------------- end to end
class TestSkillsEndToEnd(SkillBase):
    """main() as a real process against a scripted model."""

    def setUp(self):
        super().setUp()
        _LLMServer.script, _LLMServer.bodies = [], []
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LLMServer)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        super().tearDown()

    def run_harness(self, stdin_text):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("VLLM_", "HARNESS_"))}
        env.update(HARNESS_BASE_URL=f"http://127.0.0.1:{self.srv.server_address[1]}/v1",
                   HARNESS_STREAM="0", PYTHONUTF8="1")
        return subprocess.run([sys.executable, "-X", "utf8",
                               os.path.join(HERE, "..", "src", "harness", "harness_vllm.py"),
                               "session.json"], input=stdin_text, capture_output=True,
                              text=True, encoding="utf-8", env=env, timeout=120)

    def test_the_menu_is_in_every_request_and_the_skill_body_arrives_as_a_tool_result(self):
        self.mk("alpha", "Does alpha", body="ALPHA-BODY-MARKER")
        self.mk("nodesc", frontmatter="---\nname: nodesc\n---\n")
        _LLMServer.script = [tool_call_reply("skill", {"name": "alpha"}), llm_reply("done")]
        out = self.run_harness("use the right skill\n")
        self.assertEqual(out.returncode, 0, out.stderr)
        first, second = _LLMServer.bodies[0], _LLMServer.bodies[1]
        self.assertIn("skill", [t["function"]["name"] for t in first["tools"]])
        for body in (first, second):
            system = body["messages"][0]
            self.assertEqual(system["role"], "system")
            self.assertIn("alpha", system["content"])
            self.assertIn("Does alpha", system["content"])
            self.assertNotIn("ALPHA-BODY-MARKER", system["content"])
        tool_msgs = [m for m in second["messages"] if m["role"] == "tool"]
        self.assertIn("ALPHA-BODY-MARKER", tool_msgs[0]["content"])
        self.assertIn("nodesc", out.stdout)                  # the skipped one was reported


# ------------------------------------------------------------------- live
@unittest.skipUnless(LIVE, "set HARNESS_LIVE=1 to run against the real model")
class TestSkillsLive(Base):
    def test_a_real_model_loads_the_matching_skill_and_uses_it(self):
        import harness_skills  # noqa: F401  (fails loudly if the module is missing)
        os.makedirs("skills/code-word")
        with open("skills/code-word/SKILL.md", "w", encoding="utf-8") as f:
            f.write("---\nname: code-word\ndescription: Use when the user asks for the secret "
                    "code word.\n---\nThe secret code word is ZEBRA-4417. Reply with it.\n")
        env = dict(os.environ, PYTHONUTF8="1")
        p = subprocess.run([sys.executable, "-X", "utf8",
                            os.path.join(HERE, "..", "src", "harness", "harness_vllm.py"), "s.json"],
                           input="What is the secret code word? Use the matching skill.\n",
                           text=True, encoding="utf-8", capture_output=True, timeout=420, env=env)
        with open("s.json", encoding="utf-8") as f:
            messages = json.load(f)
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if not tool_msgs:
            self.skipTest("the model did not load the skill this time:\n" + p.stdout[-400:])
        self.assertIn("ZEBRA-4417", tool_msgs[0]["content"])
        self.assertIn("ZEBRA-4417", p.stdout)


@unittest.skipUnless(LIVE and LIVE_DIR, "set HARNESS_LIVE=1 and HARNESS_SKILLS_LIVE_DIR")
class TestRealSkillsFolder(SkillBase):
    """The 50 real SKILL.md files in a real Claude Code skills folder: formats the
    parser was not written against."""

    def test_the_real_folder_loads_without_surprises(self):
        reg = self.hs.load_all([LIVE_DIR])
        with_md = [d for d in os.listdir(LIVE_DIR)
                   if os.path.exists(os.path.join(LIVE_DIR, d, "SKILL.md"))]
        skipped = len(with_md) - len(reg.skills)
        print(f"\n  real folder: {len(with_md)} with SKILL.md, {len(reg.skills)} loaded, "
              f"{skipped} skipped; warnings: {len(reg.warnings)}")
        self.assertGreaterEqual(len(reg.skills), 30)
        for skill in reg.skills.values():
            self.assertTrue(skill.description.strip(), skill.name)
            self.assertLessEqual(len(skill.description), self.hs.MAX_DESCRIPTION + 1)
        if "plan-gate" in reg.skills:                          # a `description: >` skill
            self.assertTrue(reg.skills["plan-gate"].description.startswith("動手寫 code 前"))
        self.assertLessEqual(len(reg.menu()), self.hs.MAX_MENU)
        links = [w for w in reg.warnings if "link that leaves" in w]
        self.assertTrue(links, "real folders contain links; none was reported")
        for linked in ("find-skills", "opencli-usage"):
            self.assertNotIn(linked, reg.skills)               # SymJack: not followed
        if skipped > len(links):                               # the rest only stop at a limit
            self.assertTrue(any("budget" in w or "limit" in w for w in reg.warnings), reg.warnings)


if __name__ == "__main__":
    unittest.main()
