"""What may be shown or stored of text that someone else (a message sender, the model) wrote.

One definition of "unsafe" for the whole package, so the reply cleaner, the stored rule name and the
display of the `decisions` command cannot drift apart:

  * control characters (C0, DEL, C1),
  * format characters that change how text is drawn (bidirectional overrides and isolates, zero-width
    space, BOM, soft hyphen, tag characters...) -- except the zero-width (non-)joiner, which some
    scripts and emoji sequences are spelled with,
  * lone surrogates (they cannot be written as UTF-8),
  * the Unicode line and paragraph separators.

Newline and tab are "unsafe" by category but are layout the text may keep; `clean` keeps them,
`clean_line` does not.
"""
import unicodedata

KEEP_FORMAT = "‌‍"


def is_unsafe(ch):
    cat = unicodedata.category(ch)
    if cat == "Cf":
        return ch not in KEEP_FORMAT
    return cat in ("Cc", "Cs", "Zl", "Zp")


def clean(text):
    """The text without unsafe characters; newline and tab stay."""
    return "".join(ch for ch in str(text) if ch in "\n\t" or not is_unsafe(ch))


def clean_line(text):
    """The text on one line: every unsafe character, newline and tab included, is dropped."""
    return "".join(ch for ch in str(text) if not is_unsafe(ch))


def _escape(ch):
    n = ord(ch)
    if n > 0xFFFF:                                   # a JSON string spells these as a surrogate pair
        n -= 0x10000
        return "\\u%04x\\u%04x" % (0xD800 + (n >> 10), 0xDC00 + (n & 0x3FF))
    return "\\u%04x" % n


def visible(text):
    """For a table: every unsafe character, and newline, tab and carriage return, shown as \\uXXXX."""
    return "".join(_escape(ch) if (is_unsafe(ch) or ch in "\n\t\r") else ch for ch in str(text))


def json_safe(dumped):
    """For JSON written with ensure_ascii=False (so Chinese stays readable): spell the characters
    json.dumps leaves raw but that must not be shown raw as \\uXXXX. The result is still valid JSON and
    parses back to the original string. json.dumps never leaves a raw newline inside a string (it writes
    \\n), so a raw newline here is layout from `indent=` and is kept."""
    return "".join(_escape(ch) if is_unsafe(ch) and ch != "\n" else ch for ch in dumped)
