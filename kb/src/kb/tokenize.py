"""Query and document tokenizer for the lexical index.

Same token rule as the existing wiki BM25 experiment, so S1's lexical results can be compared with the old system (not fully like for like: the old
system scores only the first 400 characters of each chunk, with its own formula; see docs/S1_REPORT.md):
lower-case latin runs and digits (latin tokens must be 2+ characters unless they are digits), and CJK runs split into character
bigrams (a single CJK character stays a unigram). FTS5's own unicode61 tokenizer does not segment Chinese, so tokens are produced
here and stored space-separated.
"""
import re

LATIN = re.compile(r"[a-z0-9]+")
CJK = re.compile(r"[一-鿿]+")
TOKENIZER_ID = "kb-tok-1"


def tokens(text):
    text = text.lower()
    out = [t for t in LATIN.findall(text) if len(t) >= 2 or t.isdigit()]
    for run in CJK.findall(text):
        if len(run) == 1:
            out.append(run)
        else:
            out += [run[i:i + 2] for i in range(len(run) - 1)]
    return out
