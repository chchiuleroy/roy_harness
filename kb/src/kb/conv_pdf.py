"""PDF to text blocks, with the page and the box of every block (Gate 2 section 4.6, stage S3; module tier T2: optional, needs PyMuPDF).

Every unit is one text block of one page and carries {"type": "pdf", "page": n (1-based), "bbox": [x0, y0, x1, y1] in PDF points} so that a search
hit can be opened at the right page (H6) and the box can be shown. All units of a page share the heading path ("page n",), so a chunk never
spans two pages: a hit then names exactly one page. A paragraph that continues on the next page is cut at the page break (a stated limit).

What the converter does NOT do: it never runs anything inside the file (MuPDF extracts text only; no script, no launch action, no form is
executed), never touches the network and never asks for a password: an encrypted file is a failed conversion with a plain reason. Pages
without a text layer (scans) are counted and reported, never silently dropped: they wait for OCR (stage S5). PyMuPDF missing is not an
exception: the converter says what is missing and the source stays "not processed" (Gate 2 section 4.6, `capability`).
"""
import re

from . import conv_base as cb

MAX_PAGES = 5000
MAX_TITLE = 200
SPACES = re.compile(r"\s+")


def capability():
    """What this converter needs and whether it is there: {'available': bool, 'missing': str|None, 'version': str|None}."""
    try:
        import fitz
        return {"available": True, "missing": None, "version": getattr(fitz, "VersionBind", None)}
    except ImportError:
        return {"available": False, "missing": "PyMuPDF (python package 'pymupdf'): PDF files cannot be read until it is installed", "version": None}


class PdfConverter:
    name = "pdf"
    version = "1"
    params = {"max_pages": MAX_PAGES, "reading_order": "content_stream", "page_scope": "chunks never span pages"}

    def accepts(self, filename, head=b""):
        return filename.lower().endswith(".pdf") or head.startswith(b"%PDF-")

    def convert(self, data, hints=None):
        if cb.too_large(data):
            return cb.failed("input larger than the converter limit", bytes=len(data))
        try:
            import fitz
        except ImportError:
            return cb.failed(capability()["missing"], missing_capability="pymupdf")
        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception as e:                       # noqa: BLE001  a parser of untrusted bytes may raise any of several error types
            return cb.failed(f"not a readable PDF: {str(e)[:200]}")
        try:
            if doc.needs_pass or doc.is_encrypted:
                return cb.failed("encrypted PDF: it needs a password; kb never asks for or stores passwords", encrypted=True)
            n_pages = doc.page_count
            warnings, units = [], []
            no_text, bad_pages = [], []
            limit = min(n_pages, MAX_PAGES)
            if n_pages > MAX_PAGES:
                warnings.append(f"{n_pages} pages; only the first {MAX_PAGES} were read")
            for index in range(limit):
                page_no = index + 1
                try:
                    blocks = doc[index].get_text("blocks")
                except Exception as e:               # noqa: BLE001  one damaged page must not lose the others
                    bad_pages.append(page_no)
                    warnings.append(f"page {page_no} could not be read: {str(e)[:120]}")
                    continue
                before = len(units)
                for x0, y0, x1, y1, text, block_no, block_type in blocks:
                    if block_type != 0:
                        continue                    # picture block
                    text = SPACES.sub(" ", text).strip()
                    if not text:
                        continue
                    units.append(cb.Unit("text_block", text,
                                         {"type": "pdf", "page": page_no, "bbox": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
                                          "block": int(block_no)},
                                         (f"page {page_no}",)))
                if len(units) == before:
                    no_text.append(page_no)
            if no_text:
                warnings.append(f"{len(no_text)} of {limit} pages have no text layer (scanned or drawn pages): they wait for OCR, which is not built yet")
            title = SPACES.sub(" ", (doc.metadata or {}).get("title") or "").strip()[:MAX_TITLE]
            report = {"pages": n_pages, "pages_read": limit, "pages_without_text": len(no_text), "pages_unreadable": len(bad_pages),
                      "blocks": len(units), "needs_ocr": bool(no_text) and len(no_text) == limit}
            status = "partial" if warnings else "ok"
            return cb.ConversionResult(status, units, {"title": title} if title else {}, warnings, report)
        finally:
            doc.close()
