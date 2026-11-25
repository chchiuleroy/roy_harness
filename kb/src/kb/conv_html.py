"""HTML to text, standard library only. Nothing is fetched and nothing is executed (Gate 2 F26, T-U1):
script, style, template, noscript, svg, canvas, iframe, object, embed and head-only metadata contribute no text; external resources
(img/script/link/iframe src and href) are counted and ignored, links are recorded as data. This module imports no networking code."""
import html
import re
from html.parser import HTMLParser

from . import conv_base as cb

SKIP = {"script", "style", "template", "noscript", "svg", "canvas", "iframe", "object", "embed", "head"}
BLOCK = {"p", "div", "section", "article", "aside", "header", "footer", "main", "nav", "li", "ul", "ol", "dl", "dt", "dd", "blockquote",
         "figure", "figcaption", "tr", "table", "form", "fieldset", "address", "details", "summary", "body", "br", "hr"}
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
CHARSET = re.compile(rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)", re.I)
VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.units, self.buf, self.stack = [], [], []
        self.skip_depth = 0
        self.head_title, self.in_title = [], False
        self.heading_level, self.heading_buf = 0, []
        self.path, self.links = [], []
        self.pre_depth = 0
        self.counts = {"scripts_skipped": 0, "external_resources_ignored": 0}
        self.block_no = 0

    # -- helpers
    def _flush(self, kind="paragraph"):
        text = re.sub(r"[ \t\r\f\v]+", " ", "".join(self.buf))
        text = re.sub(r" ?\n ?", "\n", text).strip()
        self.buf = []
        if text:
            self.block_no += 1
            self.units.append(cb.Unit(kind, text, {"type": "html", "block": self.block_no, "dom": ">".join(self.stack[-6:])},
                                      tuple(self.path)))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for attr in ("src", "href", "data", "poster", "srcset"):
            if a.get(attr) and tag in ("img", "script", "link", "iframe", "embed", "source", "video", "audio", "object"):
                self.counts["external_resources_ignored"] += 1
        if tag == "a" and a.get("href") and a["href"] not in self.links and not a["href"].lower().startswith("javascript:"):
            self.links.append(a["href"])
        if tag == "script":
            self.counts["scripts_skipped"] += 1
        if tag == "title":                            # its text is the document title, never body text (even without a <head>)
            self.in_title = True
        if tag in SKIP:
            if tag not in VOID:
                self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag not in VOID:
            self.stack.append(tag)
        if tag == "pre":
            self._flush()
            self.pre_depth += 1
        if tag in HEADINGS:
            self._flush()
            self.heading_level, self.heading_buf = HEADINGS[tag], []
        elif tag in BLOCK:
            self._flush("code" if self.pre_depth else "paragraph")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in SKIP:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag in HEADINGS and self.heading_level:
            text = re.sub(r"\s+", " ", "".join(self.heading_buf)).strip()
            if text:
                self.path[:] = self.path[:self.heading_level - 1] + [text]
                self.block_no += 1
                self.units.append(cb.Unit("heading", text, {"type": "html", "block": self.block_no, "dom": ">".join(self.stack[-6:])},
                                          tuple(self.path)))
            self.heading_level, self.heading_buf = 0, []
        elif tag == "pre":
            self._flush("code")
            self.pre_depth = max(0, self.pre_depth - 1)
        elif tag in BLOCK:
            self._flush()
        if tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass

    def handle_data(self, data):
        if self.in_title:
            self.head_title.append(data)
            return
        if self.skip_depth:
            return
        if self.heading_level:
            self.heading_buf.append(data)
        else:
            self.buf.append(data if self.pre_depth else data.replace("\n", " "))

    def close(self):
        super().close()
        self._flush()


class HtmlConverter:
    name = "html"
    version = "1"
    params = {}

    def accepts(self, filename, head=b""):
        low = filename.lower()
        return low.endswith((".html", ".htm", ".xhtml")) or head.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html"))

    def convert(self, data, hints=None):
        if cb.too_large(data):
            return cb.failed("input larger than the converter limit", bytes=len(data))
        warnings = []
        m = CHARSET.search(data[:4096])
        declared = m.group(1).decode("ascii", "ignore").lower() if m else None
        try:
            if data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
                raise LookupError
            text, enc = (data.decode(declared), declared) if declared else (None, None)
        except (LookupError, UnicodeDecodeError, TypeError):
            text, enc = None, None
        if text is None:
            text, enc, warnings = cb.decode_text(data)
        p = _Extractor()
        p.feed(text)
        p.close()
        title = re.sub(r"\s+", " ", html.unescape("".join(p.head_title))).strip() or None
        meta = {"encoding": enc, "title": title, "links": p.links}
        report = {"encoding": enc, "units": len(p.units), "links": len(p.links), **p.counts}
        flags = []
        if p.counts["scripts_skipped"]:
            flags.append("scripts_skipped")
        if p.counts["external_resources_ignored"]:
            flags.append("external_resources_ignored")
        report["risk_flags"] = flags
        return cb.ConversionResult("partial" if warnings else "ok", p.units, meta, warnings, report)
