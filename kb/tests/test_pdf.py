"""S3a: the PDF converter and its place in the pipeline (T-H6 for PDF needs the page and the box; H5 needs failures to be visible)."""
import socket
import sys
import unittest
from unittest import mock

import fitz

from kb import commit, conv_pdf, conv_registry, search
from kb.conv_pdf import PdfConverter

from .world import World


def make_pdf(pages, title=None, password=None):
    """pages: list of lists of (rect, text, fontname); an empty list is a page without any text."""
    doc = fitz.open()
    for blocks in pages:
        page = doc.new_page()
        for rect, text, font in blocks:
            page.insert_textbox(fitz.Rect(*rect), text, fontsize=11, fontname=font)
    if title:
        doc.set_metadata({"title": title})
    kw = {"encryption": fitz.PDF_ENCRYPT_AES_256, "owner_pw": "owner", "user_pw": password} if password else {}
    return doc.tobytes(**kw)


def block(y, text, font="helv"):
    return ((72, y, 520, y + 60), text, font)


TWO_PAGES = make_pdf([[block(80, "The falcon flies over the harbor."), block(200, "Second block on the first page.")],
                      [block(80, "Page two talks about quantization sensitivity.")]], title="Falcon notes")


class Registered(unittest.TestCase):
    def setUp(self):
        super().setUp()
        saved = list(conv_registry.CONVERTERS)
        self.addCleanup(lambda: conv_registry.CONVERTERS.__setitem__(slice(None), saved))
        conv_registry.register(PdfConverter())


class TestConverter(unittest.TestCase):
    def convert(self, data):
        return PdfConverter().convert(data, {"filename": "x.pdf"})

    def test_the_box_is_the_box_of_the_block_not_a_mix_of_its_corners(self):
        res = self.convert(make_pdf([[block(100, "One long line of located text that is much wider than it is tall")]]))
        x0, y0, x1, y1 = res.units[0].locator["bbox"]
        self.assertTrue(70 <= x0 and x1 <= 522 and 98 <= y0 and y1 <= 162, (x0, y0, x1, y1))
        self.assertGreater(x1 - x0, 150)                                                  # a line of text is wide ...
        self.assertLess(y1 - y0, 30)                                                      # ... and one line high

    def test_a_picture_block_is_never_text_whatever_flags_the_library_uses(self):
        real = fitz.Page.get_text

        def with_a_picture(page, *a, **k):
            return list(real(page, *a, **k)) + [(10.0, 300.0, 200.0, 428.0, "<image: RGB, width: 40, height: 40>" + chr(10), 9, 1)]
        with mock.patch.object(fitz.Page, "get_text", with_a_picture):
            res = self.convert(TWO_PAGES)
        self.assertEqual(len(res.units), 3)
        self.assertFalse(any("image" in u.text for u in res.units))

    def test_every_unit_names_its_page_and_box_and_the_box_lies_on_that_page(self):
        res = self.convert(TWO_PAGES)
        self.assertEqual(res.status, "ok")
        self.assertEqual([u.locator["page"] for u in res.units], [1, 1, 2])
        for u in res.units:
            x0, y0, x1, y1 = u.locator["bbox"]
            self.assertTrue(0 <= x0 < x1 <= 612 and 0 <= y0 < y1 <= 792, u.locator)       # US Letter page, in PDF points
            self.assertEqual(u.locator["type"], "pdf")
        self.assertIn("falcon", res.units[0].text)
        self.assertIn("quantization", res.units[2].text)

    def test_a_page_is_one_section_so_a_chunk_can_never_span_two_pages(self):
        res = self.convert(TWO_PAGES)
        self.assertEqual([u.heading_path for u in res.units], [("page 1",), ("page 1",), ("page 2",)])

    def test_same_bytes_same_units(self):
        a, b = self.convert(TWO_PAGES), self.convert(TWO_PAGES)
        self.assertEqual([(u.text, u.locator, u.heading_path) for u in a.units], [(u.text, u.locator, u.heading_path) for u in b.units])

    def test_title_comes_from_the_metadata_and_is_bounded(self):
        self.assertEqual(self.convert(TWO_PAGES).meta, {"title": "Falcon notes"})
        long = make_pdf([[block(80, "x")]], title="T" * 500)
        self.assertEqual(len(self.convert(long).meta["title"]), conv_pdf.MAX_TITLE)
        self.assertEqual(self.convert(make_pdf([[block(80, "x")]])).meta, {})

    def test_chinese_text_survives_with_its_page(self):
        res = self.convert(make_pdf([[block(80, "獵鷹飛過港口。", "china-t")]]))
        self.assertEqual(res.units[0].text, "獵鷹飛過港口。")
        self.assertEqual(res.units[0].locator["page"], 1)

    def test_a_page_without_text_is_counted_and_warned_about_not_dropped_silently(self):
        res = self.convert(make_pdf([[block(80, "Text here.")], [], [block(80, "More text.")]]))
        self.assertEqual(res.status, "partial")
        self.assertEqual(res.report["pages"], 3)
        self.assertEqual(res.report["pages_without_text"], 1)
        self.assertFalse(res.report["needs_ocr"])
        self.assertTrue(any("1 of 3 pages have no text layer" in w for w in res.warnings))
        self.assertEqual([u.locator["page"] for u in res.units], [1, 3])                   # page numbers are the real ones, not renumbered

    def test_a_file_with_no_text_at_all_says_it_needs_ocr(self):
        res = self.convert(make_pdf([[], []]))
        self.assertEqual(res.units, [])
        self.assertTrue(res.report["needs_ocr"])
        self.assertEqual(res.status, "partial")

    def test_an_encrypted_file_fails_with_a_plain_reason_and_never_asks_for_a_password(self):
        res = self.convert(make_pdf([[block(80, "secret words")]], password="pw"))
        self.assertEqual(res.status, "failed")
        self.assertIn("encrypted", res.warnings[0])
        self.assertEqual(res.units, [])

    def test_bytes_that_are_not_a_pdf_fail_without_raising(self):
        for data in (b"", b"hello", b"%PDF-1.7\n%garbage", TWO_PAGES[:200]):
            res = self.convert(data)
            self.assertEqual(res.status, "failed", data[:20])
            self.assertTrue(res.warnings)

    def test_one_unreadable_page_does_not_lose_the_others(self):
        real = fitz.Page.get_text

        def flaky(page, *a, **k):
            if page.number == 1:
                raise RuntimeError("damaged page")
            return real(page, *a, **k)
        with mock.patch.object(fitz.Page, "get_text", flaky):
            res = self.convert(make_pdf([[block(80, "first")], [block(80, "second")], [block(80, "third")]]))
        self.assertEqual(res.status, "partial")
        self.assertEqual([u.locator["page"] for u in res.units], [1, 3])
        self.assertEqual(res.report["pages_unreadable"], 1)
        self.assertTrue(any("page 2 could not be read" in w for w in res.warnings))

    def test_line_breaks_inside_a_block_become_single_spaces(self):
        res = self.convert(make_pdf([[block(80, "alpha beta" + chr(10) + "gamma delta")]]))
        self.assertEqual(res.units[0].text, "alpha beta gamma delta")

    def test_a_page_limit_is_a_visible_warning(self):
        with mock.patch.object(conv_pdf, "MAX_PAGES", 1):
            res = self.convert(TWO_PAGES)
        self.assertEqual(res.status, "partial")
        self.assertEqual({u.locator["page"] for u in res.units}, {1})
        self.assertTrue(any("only the first 1 were read" in w for w in res.warnings))
        self.assertEqual(res.report["pages"], 2)                                         # the report still says how many pages the file has

    def test_without_pymupdf_the_converter_reports_what_is_missing_instead_of_raising(self):
        with mock.patch.dict(sys.modules, {"fitz": None}):
            res = self.convert(TWO_PAGES)
            cap = conv_pdf.capability()
        self.assertEqual(res.status, "failed")
        self.assertIn("PyMuPDF", res.warnings[0])
        self.assertFalse(cap["available"])
        self.assertIn("PyMuPDF", cap["missing"])

    def test_conversion_opens_no_network_connection(self):
        def refuse(*a, **k):
            raise AssertionError("a PDF conversion tried to use the network")
        with mock.patch.object(socket, "socket", refuse), mock.patch.object(socket, "create_connection", refuse):
            self.assertEqual(self.convert(TWO_PAGES).status, "ok")

    def test_the_file_is_recognised_by_name_or_by_its_first_bytes(self):
        c = PdfConverter()
        self.assertTrue(c.accepts("a.PDF"))
        self.assertTrue(c.accepts("noextension", b"%PDF-1.7"))
        self.assertFalse(c.accepts("a.txt", b"hello"))


class TestInThePipeline(Registered, World):
    def test_a_pdf_is_searchable_and_the_hit_names_its_page_and_box(self):
        self.add("papers/falcon.pdf", TWO_PAGES)
        self.build()
        hits = search.Searcher(self.home, self.conn).chunks("quantization sensitivity")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].locator["page"], 2)
        self.assertEqual(len(hits[0].locator["bbox"]), 4)
        self.assertEqual(hits[0].title, "Falcon notes")

    def test_no_chunk_spans_two_pages(self):
        same = "alpha beta gamma delta"
        self.add("same.pdf", make_pdf([[block(80, same)], [block(80, same)], [block(80, same)]]))
        self.build()
        rows = self.conn.execute("SELECT c.chunk_id, c.unit_version_ids_json FROM commit_chunks cc JOIN chunks c ON c.chunk_id = cc.chunk_id "
                                 "WHERE cc.commit_id = ?", (commit.active_commit_id(self.conn),)).fetchall()
        pages = []
        for _, uvs in rows:
            import json
            ids = json.loads(uvs)
            locs = {json.loads(self.one("SELECT locator_json FROM conversion_units WHERE unit_version_id = ?", u)[0])["page"] for u in ids}
            self.assertEqual(len(locs), 1, "a chunk covers units from more than one page")
            pages.append(locs.pop())
        self.assertEqual(sorted(pages), [1, 2, 3])

    def test_an_encrypted_pdf_is_a_visible_failed_conversion_not_a_silent_gap(self):
        self.add("locked.pdf", make_pdf([[block(80, "secret words")]], password="pw"))
        self.build()
        row = self.one("SELECT status, report_json FROM conversions WHERE converter = 'pdf'")
        self.assertEqual(row[0], "failed")
        self.assertIn("encrypted", row[1])
        self.assertEqual(self.search("secret"), [])

    def test_a_pdf_without_a_registered_converter_is_an_unsupported_type_not_a_crash(self):
        conv_registry.CONVERTERS[:] = [c for c in conv_registry.CONVERTERS if c.name != "pdf"]
        self.add("plain.pdf", TWO_PAGES)
        self.build()
        self.assertEqual(self.one("SELECT status, report_json FROM conversions")[0], "failed")
        self.assertIn("unsupported_type", self.one("SELECT report_json FROM conversions")[0])


if __name__ == "__main__":
    unittest.main()
