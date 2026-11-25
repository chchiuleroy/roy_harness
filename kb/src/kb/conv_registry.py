"""Which converter handles which file. Unsupported types are not an error: the caller records them as unsupported (visible in status)."""
from .conv_csv import CsvConverter
from .conv_html import HtmlConverter
from .conv_markdown import MarkdownConverter
from .conv_text import TextConverter

CONVERTERS = [MarkdownConverter(), HtmlConverter(), CsvConverter(), TextConverter()]


def register(converter):
    """Add an optional converter (tier T2, e.g. PDF) at run time: this module is tier T0 and may not import it. Registering the same name again
    replaces the earlier one, so repeated start-up does not stack duplicates."""
    CONVERTERS[:] = [c for c in CONVERTERS if c.name != converter.name] + [converter]


def pick(filename, head=b""):
    for c in CONVERTERS:
        if c.accepts(filename, head):
            return c
    return None


def by_name(name):
    for c in CONVERTERS:
        if c.name == name:
            return c
    return None
