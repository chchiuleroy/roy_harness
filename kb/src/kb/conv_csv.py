"""CSV/TSV to row groups. Every cell stays TEXT: a cell that starts with = + - @ (a spreadsheet formula) is stored as written and
counted in the report, never evaluated (Gate 2 F26, T-U2). escape_cell() is the matching rule for any future export.
Each unit is a group of rows rendered as 'column: value; ...' lines so the header travels with the values."""
import csv
import io
import re

from . import conv_base as cb

GROUP_ROWS = 20
MAX_ROWS = 1_000_000
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


CANDIDATES = (",", "\t", ";", "|")


def choose_delimiter(text, filename=""):
    """The delimiter that splits the first lines into the same number (> 1) of fields most consistently wins; ties go to the
    order , tab ; | (and a .tsv name prefers tab). csv.Sniffer was dropped: a formula cell containing '|' fooled it."""
    head = text[:20000].split("\n")[:50]
    best, best_score = ",", (0, 0)
    for d in CANDIDATES:
        try:
            counts = [len(r) for r in csv.reader(head, delimiter=d) if r]
        except csv.Error:
            continue
        if not counts:
            continue
        top = max(set(counts), key=counts.count)
        score = (counts.count(top) if top > 1 else 0, top)
        if filename.lower().endswith(".tsv") and d == "\t":
            score = (score[0] + 1, score[1])
        if score > best_score:
            best, best_score = d, score
    return best


NUMBER = re.compile(r"^[+-]?[\d.,]+(e[+-]?\d+)?%?$", re.I)


def looks_like_formula(cell):
    """For the report only: '=' and '@' always, '+'/'-' unless the cell is just a signed number."""
    return cell.startswith(("=", "@")) or (cell[:1] in ("+", "-") and not NUMBER.match(cell))


def escape_cell(value):
    """Prefix a formula-looking cell with an apostrophe so that a spreadsheet opens it as text."""
    return "'" + value if value.startswith(FORMULA_START) else value


class CsvConverter:
    name = "csv"
    version = "1"
    params = {"group_rows": GROUP_ROWS, "max_rows": MAX_ROWS}

    def accepts(self, filename, head=b""):
        return filename.lower().endswith((".csv", ".tsv"))

    def convert(self, data, hints=None):
        if cb.too_large(data):
            return cb.failed("input larger than the converter limit", bytes=len(data))
        text, enc, warnings = cb.decode_text(data)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        dialect = type("D", (csv.excel,), {"delimiter": choose_delimiter(text, (hints or {}).get("filename", ""))})
        csv.field_size_limit(10_000_000)
        try:
            rows = []
            for row in csv.reader(io.StringIO(text), dialect):
                if len(rows) >= MAX_ROWS:                      # check BEFORE keeping the row, so the cap is exact
                    warnings.append(f"more than {MAX_ROWS} rows; the rest was not read")
                    break
                rows.append(row)
        except csv.Error as e:
            return cb.failed(f"not parseable as CSV: {e}")
        if not rows:
            return cb.ConversionResult("ok", [], {"encoding": enc}, warnings, {"rows": 0, "encoding": enc})
        header = [h.strip() or f"col{i + 1}" for i, h in enumerate(rows[0])]
        body = rows[1:]
        formulas = sum(1 for r in rows for c in r if looks_like_formula(c))
        units = []
        for a in range(0, len(body), GROUP_ROWS):
            chunk = body[a:a + GROUP_ROWS]
            lines = ["; ".join(f"{header[i] if i < len(header) else f'col{i + 1}'}: {v}" for i, v in enumerate(r) if v.strip())
                     for r in chunk]
            units.append(cb.Unit("row_group", "\n".join(l for l in lines if l),
                                 {"type": "csv", "rows": [a + 2, a + 1 + len(chunk)], "header": header[:50]}))
        units = [u for u in units if u.text]
        risk = ["formula_cells_kept_as_text"] if formulas else []
        status = "partial" if warnings else "ok"
        return cb.ConversionResult(status, units, {"encoding": enc, "columns": header[:200]}, warnings,
                                   {"rows": len(rows), "columns": len(header), "formula_cells": formulas, "encoding": enc,
                                    "delimiter": getattr(dialect, "delimiter", ","), "risk_flags": risk})
