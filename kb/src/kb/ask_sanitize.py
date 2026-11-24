"""What may be shown or stored of text that somebody else wrote (a document, a question, the model).

One definition of "unsafe" for the whole question-and-answer layer (stage S4), so that the framing, the screening, the stored answer and the
display cannot drift apart. Unsafe: control characters (C0, DEL, C1); format characters that change how text is drawn (bidirectional
overrides and isolates, zero-width space, BOM, soft hyphen, tag characters) except the zero-width (non-)joiner, which some scripts and emoji
sequences are spelled with; lone surrogates (they cannot be written as UTF-8); the Unicode line and paragraph separators.
Newline and tab are layout and stay in `clean`; `clean_line` drops them too.
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


def count_unsafe(text):
    """How many characters of `text` are unsafe in the sense above (newline and tab not counted)."""
    return sum(1 for ch in str(text) if ch not in "\n\t" and is_unsafe(ch))
