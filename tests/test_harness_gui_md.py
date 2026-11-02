"""Tests for src/harness/harness_gui_md.js, run with Node.

The page shows model output, which is untrusted. The parser turns text into a plain tree
(blocks and inline pieces, no HTML anywhere); the page builds DOM nodes from that tree with
textContent only. So the thing to prove is: nothing the model writes can become markup, and a
hostile input cannot make the parser slow.

Run:  python -X utf8 -m unittest discover -s tests -v   (skipped when Node is not installed)
"""
import json
import os
import shutil
import subprocess
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MD = os.path.abspath(os.path.join(HERE, "..", "src", "harness", "harness_gui_md.js")).replace("\\", "/")
NODE = shutil.which("node")
RUNNER = ("const {parseMarkdown} = require(%s);"
          "process.stdout.write(JSON.stringify(parseMarkdown(require('fs').readFileSync(0, 'utf8'))));"
          % json.dumps(MD))


@unittest.skipUnless(NODE, "Node is not installed")
class TestMarkdown(unittest.TestCase):
    def parse(self, text):
        # bytes both ways: on Windows text mode would turn every \n into \r\n on the way in
        out = subprocess.run([NODE, "-e", RUNNER], input=text.encode("utf-8"), capture_output=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        return json.loads(out.stdout.decode("utf-8"))

    def run_js(self, expression):
        script = "const m = require(%s); with (m) { process.stdout.write(JSON.stringify(%s)); }" % (json.dumps(MD), expression)
        out = subprocess.run([NODE, "-e", script], capture_output=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        return json.loads(out.stdout.decode("utf-8"))

    @staticmethod
    def inline_types(blocks):
        found = set()
        for b in blocks:
            for part in (b.get("inline") or []):
                found.add(part["t"])
            for item in (b.get("items") or []):
                found.update(part["t"] for part in item)
        return found

    def test_bold_and_inline_code(self):
        blocks = self.parse("a **b** `c` d")
        self.assertEqual(blocks, [{"type": "para", "inline": [
            {"t": "text", "v": "a "}, {"t": "strong", "v": "b"}, {"t": "text", "v": " "},
            {"t": "code", "v": "c"}, {"t": "text", "v": " d"}]}])

    def test_headings_and_paragraphs(self):
        blocks = self.parse("## Title\nfirst line\nsecond line\n\nnext paragraph")
        self.assertEqual([b["type"] for b in blocks], ["heading", "para", "para"])
        self.assertEqual(blocks[0]["level"], 2)
        self.assertEqual(blocks[1]["inline"], [{"t": "text", "v": "first line\nsecond line"}])

    def test_bullet_and_numbered_lists_are_separate_lists(self):
        blocks = self.parse("- a\n- b **x**\n\n1. one\n2. two")
        self.assertEqual([(b["type"], b.get("ordered"), len(b["items"])) for b in blocks],
                         [("list", False, 2), ("list", True, 2)])
        self.assertEqual(blocks[0]["items"][1], [{"t": "text", "v": "b "}, {"t": "strong", "v": "x"}])

    def test_a_fenced_block_keeps_its_text_and_drops_the_language_line(self):
        blocks = self.parse("before\n```python\nprint(1)\n```\nafter")
        self.assertEqual([b["type"] for b in blocks], ["para", "code", "para"])
        self.assertEqual(blocks[1]["text"], "print(1)\n")

    def test_markdown_inside_a_fence_is_not_interpreted(self):
        blocks = self.parse("```\n**x** `y` # z\n```")
        self.assertEqual(blocks, [{"type": "code", "text": "**x** `y` # z\n"}])

    def test_an_unfinished_fence_while_streaming_is_already_a_code_block(self):
        blocks = self.parse("here:\n```python\nprint(")
        self.assertEqual([b["type"] for b in blocks], ["para", "code"])
        self.assertEqual(blocks[1]["text"], "print(")

    def test_html_and_script_stay_text(self):
        """The reason for the whole design: markup written by a model is just characters."""
        hostile = ('<img src=x onerror=alert(1)> <script>alert(2)</script> '
                   '[click](javascript:alert(3)) <a href="javascript:alert(4)">x</a>')
        blocks = self.parse(hostile)
        self.assertEqual(blocks, [{"type": "para", "inline": [{"t": "text", "v": hostile}]}])
        self.assertEqual(self.inline_types(blocks), {"text"})

    def test_unclosed_markers_stay_literal(self):
        blocks = self.parse("**not closed and `neither")
        self.assertEqual(blocks, [{"type": "para", "inline": [{"t": "text", "v": "**not closed and `neither"}]}])

    def test_windows_line_endings_do_not_break_headings_and_lists(self):
        """Text that came out of a command or a file on Windows carries \\r\\n."""
        blocks = self.parse("# Title\r\n- a\r\n- b\r\n")
        self.assertEqual([b["type"] for b in blocks], ["heading", "list"])
        self.assertEqual(blocks[0]["inline"], [{"t": "text", "v": "Title"}])
        self.assertEqual(len(blocks[1]["items"]), 2)

    def test_the_repaint_interval_grows_with_the_answer_but_stays_bounded(self):
        """The page re-parses the whole answer each time it repaints a streamed answer; the longer
        the answer, the less often it may do so (found by the review: 50,000 characters repainted
        every 10 characters took 1.7 s in total)."""
        gaps = [self.run_js("renderGap(%d)" % n) for n in (0, 1_000, 10_000, 50_000, 10_000_000)]
        self.assertEqual(gaps[0], 50)
        self.assertEqual(sorted(gaps), gaps)
        self.assertGreater(gaps[3], gaps[1])
        self.assertEqual(gaps[-1], 1000)                                   # never slower than one repaint a second

    def test_empty_input(self):
        self.assertEqual(self.parse(""), [])

    def test_hostile_input_does_not_make_the_parser_slow(self):
        """Many unclosed markers must not make the scan quadratic (the page re-parses the whole
        answer on every streamed piece)."""
        started = time.time()
        for text in ("*" * 200_000, "`" * 200_000, "**a" * 60_000, "- " * 100_000, "#" * 200_000):
            self.parse(text)
        self.assertLess(time.time() - started, 20)


PAGE = os.path.join(HERE, "..", "src", "harness", "harness_gui.html")
CHECK = ("const vm = require('vm'); const code = require('fs').readFileSync(0, 'utf8');"
         "try { new vm.Script(code); process.stdout.write('ok'); } catch (e) { process.stdout.write(e.message); }")


@unittest.skipUnless(NODE, "Node is not installed")
class TestPageSource(unittest.TestCase):
    """The page has no browser test, so what can be checked from its source is checked here."""

    def setUp(self):
        with open(PAGE, encoding="utf-8") as f:
            self.html = f.read()

    def test_the_inline_script_is_valid_javascript(self):
        import re
        code = re.search(r'<script nonce="__NONCE__">([\s\S]*?)</script>', self.html).group(1)
        out = subprocess.run([NODE, "-e", CHECK], input=code.encode("utf-8"), capture_output=True, timeout=60)
        self.assertEqual(out.stdout.decode("utf-8"), "ok")

    def test_there_is_no_style_attribute_for_the_csp_to_block(self):
        """The CSP allows styles only with the page's nonce, which an attribute cannot carry: a
        style="..." in the markup is silently ignored (a row of two controls once showed as two rows)."""
        import re
        self.assertEqual(re.findall(r'\sstyle="[^"]*"', self.html), [])

    def test_every_element_the_script_looks_up_exists(self):
        import re
        used = set(re.findall(r'\$\("([A-Za-z]+)"\)', self.html))
        defined = set(re.findall(r'\sid="([A-Za-z]+)"', self.html))
        self.assertEqual(sorted(used - defined), [])

    def test_a_view_that_stops_following_is_woken_when_its_session_runs_again(self):
        """The server ends the stream of a session that is not running, so the page parks its view.
        A parked view with nobody to wake it would stay silent after 'resume' (here or elsewhere)."""
        import re
        follow = re.search(r"async function follow\(v\) \{[\s\S]*?\n\}\n", self.html).group(0)
        self.assertIn("v.parked = true", follow)
        self.assertIn("wake(v)", re.search(r"function showResume[\s\S]*?\n\}\n", self.html).group(0))
        self.assertIn("wake(v)", re.search(r"async function refresh\(\) \{[\s\S]*?\n\}\n", self.html).group(0))

    def test_the_page_never_writes_html(self):
        # these are only strings searched for in the page's source: nothing here evaluates anything
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(banned, self.html)
        with open(MD, encoding="utf-8") as f:
            md = f.read()
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(banned, md)


if __name__ == "__main__":
    unittest.main()
