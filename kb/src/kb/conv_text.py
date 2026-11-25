"""Plain text: paragraphs separated by blank lines."""
import re

from . import conv_base as cb

BLANK = re.compile(r"\n[ \t]*\n+")


class TextConverter:
    name = "text"
    version = "1"
    params = {}

    def accepts(self, filename, head=b""):
        return filename.lower().endswith((".txt", ".text", ".log"))

    def convert(self, data, hints=None):
        if cb.too_large(data):
            return cb.failed("input larger than the converter limit", bytes=len(data))
        text, enc, warnings = cb.decode_text(data)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        units, pos = [], 0
        for m in list(BLANK.finditer(text)) + [None]:
            end = m.start() if m else len(text)
            seg = text[pos:end]
            if seg.strip():
                start = pos + (len(seg) - len(seg.lstrip()))
                stop = end - (len(seg) - len(seg.rstrip()))
                line1 = text.count("\n", 0, start) + 1
                line2 = text.count("\n", 0, stop) + 1
                units.append(cb.Unit("paragraph", text[start:stop], {"type": "text", "char": [start, stop], "line": [line1, line2]}))
            pos = m.end() if m else end
        status = "partial" if warnings else "ok"
        return cb.ConversionResult(status, units, {"encoding": enc}, warnings,
                                   {"encoding": enc, "units": len(units), "chars": len(text)})
