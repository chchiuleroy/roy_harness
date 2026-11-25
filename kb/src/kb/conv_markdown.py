"""Markdown: front matter, headings (kept as the heading path of what follows), paragraphs, lists, tables, fenced code.
Wikilinks ([[target|alias#section]]) are recorded in the metadata for the deterministic-relations step; nothing is fetched or executed."""
import re

from . import conv_base as cb

HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t#]*$")
FENCE = re.compile(r"^(```|~~~)")
LIST_ITEM = re.compile(r"^([ \t]*)([-*+]|\d+[.)])[ \t]+")
TABLE_ROW = re.compile(r"^\s*\|")
WIKILINK = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]*)?(?:\|[^\]]*)?\]\]")
KEYVAL = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$")


def parse_front_matter(lines):
    """Returns (meta, number_of_lines_consumed). Only top-level 'key: value' pairs and '- item' lists: enough for title and tags."""
    if not lines or lines[0].strip() != "---":
        return {}, 0
    meta, key = {}, None
    for i in range(1, len(lines)):
        s = lines[i].rstrip("\n")
        if s.strip() == "---":
            return meta, i + 1
        m = KEYVAL.match(s)
        if m:
            key, val = m.group(1), m.group(2).strip().strip("'\"")
            meta[key] = val if val else []
        elif key and s.lstrip().startswith("- ") and isinstance(meta.get(key), list):
            meta[key].append(s.lstrip()[2:].strip().strip("'\""))
    return {}, 0                                   # never closed: treat the dashes as ordinary text


class MarkdownConverter:
    name = "markdown"
    version = "1"
    params = {}

    def accepts(self, filename, head=b""):
        return filename.lower().endswith((".md", ".markdown", ".mdx"))

    def convert(self, data, hints=None):
        if cb.too_large(data):
            return cb.failed("input larger than the converter limit", bytes=len(data))
        text, enc, warnings = cb.decode_text(data)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.splitlines(keepends=True)
        offsets, acc = [], 0
        for ln in lines:
            offsets.append(acc)
            acc += len(ln)
        fm, used = parse_front_matter(lines)
        units, path, i, n = [], [], used, len(lines)

        def emit(kind, a, b):
            seg = "".join(lines[a:b]).strip("\n")
            if seg.strip():
                units.append(cb.Unit(kind, seg, {"type": "md", "char": [offsets[a], offsets[b - 1] + len(lines[b - 1].rstrip("\n"))],
                                                  "line": [a + 1, b]}, tuple(path)))

        while i < n:
            s = lines[i].rstrip("\n")
            if not s.strip():
                i += 1
                continue
            if FENCE.match(s):
                j = i + 1
                while j < n and not lines[j].startswith(s[:3]):
                    j += 1
                emit("code", i, min(j + 1, n))
                i = j + 1
                continue
            h = HEADING.match(s)
            if h:
                level = len(h.group(1))
                path[:] = path[:level - 1] + [h.group(2).strip()]
                emit("heading", i, i + 1)
                i += 1
                continue
            kind = "table" if TABLE_ROW.match(s) else ("list" if LIST_ITEM.match(s) else "paragraph")
            j = i + 1
            while j < n and lines[j].strip() and not HEADING.match(lines[j].rstrip("\n")) and not FENCE.match(lines[j]):
                nxt = lines[j].rstrip("\n")
                if kind == "table" and not TABLE_ROW.match(nxt):
                    break
                if kind == "list" and not (LIST_ITEM.match(nxt) or nxt.startswith((" ", "\t"))):
                    break
                if kind == "paragraph" and (TABLE_ROW.match(nxt) or LIST_ITEM.match(nxt)):
                    break
                j += 1
            emit(kind, i, j)
            i = j
        title = fm.get("title") if isinstance(fm.get("title"), str) and fm.get("title") else None
        if title is None:
            title = next((u.text.lstrip("# ").strip() for u in units if u.kind == "heading"), None)
        links = []
        for u in units:
            if u.kind == "code":                              # example text in a code block is not a link; front matter is not a unit
                continue
            for t in WIKILINK.findall(u.text):
                t = t.strip()
                if t and t not in links:
                    links.append(t)
        meta = {"encoding": enc, "title": title, "frontmatter": fm, "links": links}
        status = "partial" if warnings else "ok"
        return cb.ConversionResult(status, units, meta, warnings,
                                   {"encoding": enc, "units": len(units), "chars": len(text), "wikilinks": len(links),
                                    "frontmatter_keys": sorted(fm)})
