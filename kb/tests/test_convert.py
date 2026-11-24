"""S1a: tokenizer and converters (T-W2 rerun stability, T-W3 conversion report, T-K input side, T-U1 and T-U2 unsafe input).
Invariant checked everywhere: a unit's locator must lead back to exactly its text in the decoded source."""
import ast
import os
import socket
import unittest

from kb import conv_base, conv_csv, conv_registry
from kb import tokenize as tk
from kb.conv_csv import CsvConverter, escape_cell
from kb.conv_html import HtmlConverter
from kb.conv_markdown import MarkdownConverter
from kb.conv_text import TextConverter

from .support import SRC


def texts(res):
    return [u.text for u in res.units]


class TestTokenizer(unittest.TestCase):
    def test_rule_matches_the_old_wiki_bm25_tokenizer(self):
        """Like-for-like comparison with the existing system depends on this rule staying identical."""
        # order is part of the old rule: latin and digit tokens first, then CJK n-grams (order does not matter for BM25 sums)
        self.assertEqual(tk.tokens("Hawkes process 維修排程 v2 x"), ["hawkes", "process", "v2", "維修", "修排", "排程"])
        self.assertEqual(tk.tokens("7 a bb"), ["7", "bb"])                    # a lone digit stays, a lone letter does not
        self.assertEqual(tk.tokens("水 and 火火"), ["and", "水", "火火"])        # a single CJK character is a unigram
        self.assertEqual(tk.tokens(""), [])

    def test_cjk_runs_do_not_bridge_latin_text(self):
        self.assertEqual(tk.tokens("中文abc文字"), ["abc", "中文", "文字"])


class TestText(unittest.TestCase):
    def test_paragraphs_and_locators_lead_back_to_the_text(self):
        data = "First paragraph\nstill first.\n\n\nSecond one.\r\n\r\n  Third, indented.  \n".encode()
        res = TextConverter().convert(data)
        norm = data.decode().replace("\r\n", "\n")
        self.assertEqual(texts(res), ["First paragraph\nstill first.", "Second one.", "Third, indented."])
        for u in res.units:
            a, b = u.locator["char"]
            self.assertEqual(norm[a:b], u.text)
        self.assertEqual(res.units[0].locator["line"], [1, 2])
        self.assertEqual(res.status, "ok")

    def test_empty_and_whitespace_only_give_no_units(self):
        for data in (b"", b"  \n\n \t\n"):
            self.assertEqual(TextConverter().convert(data).units, [])

    def test_encodings(self):
        """Traditional Chinese files in the wild are often cp950; a guess must be reported, never silent."""
        self.assertEqual(texts(TextConverter().convert("﻿繁體中文".encode("utf-8"))), ["繁體中文"])
        self.assertEqual(texts(TextConverter().convert("utf16 文字".encode("utf-16"))), ["utf16 文字"])
        res = TextConverter().convert("繁體中文測試".encode("cp950"))
        self.assertEqual(texts(res), ["繁體中文測試"])
        self.assertEqual(res.status, "partial")
        self.assertIn("cp950", res.warnings[0])
        res = TextConverter().convert(b"\xff\xfe\x00bad\xc3(")
        self.assertIn(res.status, ("ok", "partial"))

    def test_oversize_input_fails_visibly(self):
        old = conv_base.MAX_INPUT_BYTES
        conv_base.MAX_INPUT_BYTES = 10
        self.addCleanup(setattr, conv_base, "MAX_INPUT_BYTES", old)
        res = TextConverter().convert(b"x" * 11)
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.units, [])
        self.assertIn("larger", res.warnings[0])

    def test_rerun_is_identical(self):
        """T-W2: a rule-based converter gives the same units for the same bytes, every time."""
        data = "段落一。\n\n段落二 with English.\n".encode()
        for conv in (TextConverter(), MarkdownConverter()):
            a, b = conv.convert(data), conv.convert(data)
            self.assertEqual((a.units, a.meta, a.report, a.status), (b.units, b.meta, b.report, b.status))


class TestMarkdown(unittest.TestCase):
    DOC = ("---\ntitle: My Page\ntags:\n  - wiki/concept\n  - rag\n---\n# Top\n\nIntro with [[Other Page]] and [[x|alias]] and [[Other Page#sec]].\n\n"
           "## Sub\n\n- item one\n- item two\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```python\n# not a heading\nprint(1)\n```\n\nTail paragraph.\n")

    def setUp(self):
        self.res = MarkdownConverter().convert(self.DOC.encode())

    def test_front_matter_title_tags_and_links(self):
        self.assertEqual(self.res.meta["title"], "My Page")
        self.assertEqual(self.res.meta["frontmatter"]["tags"], ["wiki/concept", "rag"])
        self.assertEqual(self.res.meta["links"], ["Other Page", "x"])           # de-duplicated, alias and section stripped
        self.assertEqual(self.res.report["wikilinks"], 2)

    def test_units_headings_and_heading_paths(self):
        kinds = [(u.kind, u.heading_path) for u in self.res.units]
        self.assertEqual(kinds[0], ("heading", ("Top",)))
        self.assertIn(("paragraph", ("Top",)), kinds)
        self.assertIn(("heading", ("Top", "Sub")), kinds)
        self.assertIn(("list", ("Top", "Sub")), kinds)
        self.assertIn(("table", ("Top", "Sub")), kinds)
        self.assertIn(("code", ("Top", "Sub")), kinds)
        self.assertEqual(kinds[-1], ("paragraph", ("Top", "Sub")))

    def test_a_hash_inside_a_code_fence_is_not_a_heading(self):
        code = [u for u in self.res.units if u.kind == "code"][0]
        self.assertIn("# not a heading", code.text)
        self.assertEqual([u.text for u in self.res.units if u.kind == "heading"], ["# Top", "## Sub"])

    def test_locators_lead_back_to_the_text(self):
        norm = self.DOC
        for u in self.res.units:
            a, b = u.locator["char"]
            self.assertEqual(norm[a:b], u.text, u.kind)

    def test_unclosed_front_matter_is_ordinary_text(self):
        res = MarkdownConverter().convert(b"---\ntitle: nope\n\nbody text\n")
        self.assertEqual(res.meta["frontmatter"], {})
        self.assertTrue(any("body text" in u.text for u in res.units))

    def test_title_falls_back_to_first_heading_then_none(self):
        self.assertEqual(MarkdownConverter().convert(b"## Real Title\n\ntext").meta["title"], "Real Title")
        self.assertIsNone(MarkdownConverter().convert(b"just text").meta["title"])

    def test_unclosed_code_fence_runs_to_the_end_without_crashing(self):
        res = MarkdownConverter().convert(b"before\n\n```\ncode never closed\nmore\n")
        self.assertEqual(res.units[-1].kind, "code")


class TestHtml(unittest.TestCase):
    PAGE = ("<!DOCTYPE html><html><head><title>Page &amp; Title</title><style>.x{color:red}</style>"
            "<script>var SECRET_CANARY='do not index me';</script></head><body>"
            "<h1>Main</h1><p>Hello <b>bold</b> world &amp; friends.</p><div>Block text<br>after break</div>"
            "<h2>Part</h2><ul><li>one</li><li>two</li></ul><pre>line1\nline2</pre>"
            "<img src='http://tracker.example/pixel.gif'><iframe src='http://evil.example/x'></iframe>"
            "<a href='https://example.com/a'>link</a><a href='javascript:alert(1)'>bad</a>"
            "<script src='http://evil.example/x.js'></script><noscript>NOSCRIPT_CANARY</noscript></body></html>")

    def run_page(self, page=None):
        return HtmlConverter().convert((page or self.PAGE).encode())

    def test_script_style_and_noscript_never_reach_the_text(self):
        """T-U1: nothing inside script/style/noscript/iframe is indexed, and the report says what was skipped."""
        res = self.run_page()
        blob = " ".join(texts(res))
        for canary in ("SECRET_CANARY", "NOSCRIPT_CANARY", "color:red", "alert(1)"):
            self.assertNotIn(canary, blob)
        self.assertEqual(res.report["scripts_skipped"], 2)
        self.assertGreaterEqual(res.report["external_resources_ignored"], 3)
        self.assertEqual(sorted(res.report["risk_flags"]), ["external_resources_ignored", "scripts_skipped"])

    def test_conversion_makes_no_network_calls(self):
        """T-U1 / H3: remote resources are counted and ignored; a socket guard proves nothing is contacted."""
        calls = []
        saved = (socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo, socket.create_connection)

        def guard(*a, **k):
            calls.append(a)
            raise OSError("network is forbidden in this test")
        socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo, socket.create_connection = guard, guard, guard, guard
        try:
            res = self.run_page()
        finally:
            socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo, socket.create_connection = saved
        self.assertEqual(calls, [])
        self.assertEqual(res.status, "ok")

    def test_module_imports_no_networking_code(self):
        """Structural guard behind the test above: the HTML converter must not even import a networking module."""
        with open(os.path.join(SRC, "kb", "conv_html.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertEqual(imported & {"socket", "urllib", "http", "requests", "ssl", "subprocess"}, set())

    def test_title_headings_blocks_links(self):
        res = self.run_page()
        self.assertEqual(res.meta["title"], "Page & Title")
        self.assertEqual(res.meta["links"], ["https://example.com/a"])        # javascript: links are dropped
        heads = [(u.text, u.heading_path) for u in res.units if u.kind == "heading"]
        self.assertEqual(heads, [("Main", ("Main",)), ("Part", ("Main", "Part"))])
        self.assertIn("Hello bold world & friends.", texts(res))
        self.assertIn("Block text", texts(res))
        self.assertIn("line1\nline2", [u.text for u in res.units if u.kind == "code"])

    def test_declared_charset_is_honoured_and_failures_are_reported(self):
        body = "<html><head><meta charset='big5'><title>繁體</title></head><body><p>中文內容測試</p></body></html>"
        res = HtmlConverter().convert(body.encode("big5"))
        self.assertEqual(texts(res), ["中文內容測試"])
        self.assertEqual(res.meta["title"], "繁體")

    def test_malformed_html_does_not_crash(self):
        res = HtmlConverter().convert(b"<p>unclosed <b>bold <div>mixed </p> text</i><<>> &bogus; <script>never closed")
        self.assertIn(res.status, ("ok", "partial"))
        self.assertTrue(any("unclosed" in t for t in texts(res)))

    def test_title_outside_a_head_is_still_only_a_title(self):
        res = HtmlConverter().convert(b"<title>Lonely</title><p>body</p>")
        self.assertEqual(res.meta["title"], "Lonely")
        self.assertEqual(texts(res), ["body"])


class TestCsv(unittest.TestCase):
    def test_header_travels_with_every_row_group(self):
        rows = ["name,qty,note"] + [f"item{i},{i},ok" for i in range(45)]
        res = CsvConverter().convert("\n".join(rows).encode())
        self.assertEqual(len(res.units), 3)                                    # 45 rows in groups of 20
        self.assertTrue(res.units[0].text.startswith("name: item0; qty: 0; note: ok"))
        self.assertEqual(res.units[1].locator["rows"], [22, 41])               # file rows, header is row 1
        self.assertEqual(res.report["rows"], 46)

    def test_formula_cells_stay_text_and_are_reported(self):
        """T-U2: a cell like =HYPERLINK(...) is indexed as the characters it is, never evaluated; the report flags it."""
        data = 'a,b\n=cmd|"/c calc"!A0,5\n+SUM(A1:A2),-7\n@evil,"-3.5"\n'.encode()
        res = CsvConverter().convert(data)
        self.assertIn("a: =cmd|", res.units[0].text)
        self.assertEqual(res.report["formula_cells"], 3)                       # =, +SUM, @evil; plain negative numbers do not count
        self.assertEqual(res.report["risk_flags"], ["formula_cells_kept_as_text"])

    def test_escape_cell_for_export(self):
        for risky in ("=1+1", "+1", "-1", "@x", "\tx", "\rx"):
            self.assertEqual(escape_cell(risky), "'" + risky)
        for safe in ("plain", "1+1", "", "a=b"):
            self.assertEqual(escape_cell(safe), safe)

    def test_delimiters_quotes_and_embedded_newlines(self):
        res = CsvConverter().convert('x;y\n"multi\nline";2\n3;4\n'.encode())
        self.assertIn("x: multi\nline; y: 2", res.units[0].text)
        res = CsvConverter().convert("k\tv\nA\t1\n".encode())
        self.assertIn("k: A; v: 1", res.units[0].text)

    def test_empty_file_and_row_cap(self):
        self.assertEqual(CsvConverter().convert(b"").units, [])
        old = conv_csv.MAX_ROWS
        conv_csv.MAX_ROWS = 5
        self.addCleanup(setattr, conv_csv, "MAX_ROWS", old)
        res = CsvConverter().convert(("h\n" + "\n".join(str(i) for i in range(50))).encode())
        self.assertEqual(res.status, "partial")
        self.assertIn("rest was not read", res.warnings[0])


class TestRegistry(unittest.TestCase):
    def test_pick_by_extension_and_sniff_and_unsupported(self):
        self.assertEqual(conv_registry.pick("a.md").name, "markdown")
        self.assertEqual(conv_registry.pick("A.HTML").name, "html")
        self.assertEqual(conv_registry.pick("t.tsv").name, "csv")
        self.assertEqual(conv_registry.pick("n.txt").name, "text")
        self.assertEqual(conv_registry.pick("noext", b"<!DOCTYPE html><html>").name, "html")
        for unsupported in ("x.docx", "x.png", "x.exe", "x.zip", "x.mp3"):
            self.assertIsNone(conv_registry.pick(unsupported))

    def test_execution_identity_records_converter_version_and_params(self):
        a = conv_base.exec_identity(CsvConverter(), CsvConverter.params)
        self.assertEqual((a["converter"], a["version"]), ("csv", "1"))
        self.assertNotEqual(a["params_hash"], conv_base.exec_identity(CsvConverter(), {"group_rows": 5})["params_hash"])
        self.assertEqual(a["params_hash"], conv_base.exec_identity(CsvConverter(), CsvConverter.params)["params_hash"])


if __name__ == "__main__":
    unittest.main()
