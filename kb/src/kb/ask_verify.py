"""Read the model's answer and check every claim's quote against the passage it names (stage S4, defence step 1).

This stops INVENTED support. It does not stop a true quote used misleadingly: nothing here can, and the layer says so. A quote counts when,
after Unicode normalisation (NFC) and collapsing runs of whitespace, it is a literal substring of the named passage and is long enough to mean
something. Everything the model wrote is data: nothing in the answer is ever executed or used to route anything.
"""
import json
import re
import unicodedata

MIN_QUOTE = 8            # characters, after normalisation; a shorter "quote" matches almost anything
MAX_CLAIMS = 40
_WS = re.compile(r"\s+")


def normalize(text):
    return _WS.sub(" ", unicodedata.normalize("NFC", str(text))).strip()


def _first_object(text):
    """The first JSON object in `text` (the model may wrap it in a code fence or a sentence), or None."""
    decoder = json.JSONDecoder()
    pos = text.find("{")
    while pos != -1:
        try:
            obj, _ = decoder.raw_decode(text, pos)
        except ValueError:
            pos = text.find("{", pos + 1)
            continue
        if isinstance(obj, dict):
            return obj
        pos = text.find("{", pos + 1)
    return None


def parse_answer(text):
    """-> (answer, claims, problem). `problem` is a fixed sentence when the reply cannot be used as an answer with claims; `answer` may still
    be the raw text so that the person sees what the model said (it is data, and stays marked as such)."""
    obj = _first_object(str(text or ""))
    if obj is None:
        return None, [], "the reply is not a JSON object"
    answer = obj.get("answer")
    claims = obj.get("claims", [])
    if not isinstance(answer, str) or not isinstance(claims, list):
        return None, [], "the JSON has no text answer or no claim list"
    good = []
    for c in claims[:MAX_CLAIMS]:
        if isinstance(c, dict) and all(isinstance(c.get(k), str) for k in ("text", "passage", "quote")):
            good.append({"text": c["text"], "passage": c["passage"], "quote": c["quote"]})
        else:
            good.append({"text": "", "passage": "", "quote": "", "malformed": True})
    problem = None
    if len(claims) > MAX_CLAIMS:
        problem = f"more than {MAX_CLAIMS} claims; the rest were not checked"
    return answer, good, problem


def verify(claims, passages):
    """`passages`: dict pid -> text. -> list of claims, each with `verified` (bool) and, when false, a fixed `reason`."""
    normalized = {pid: normalize(t) for pid, t in passages.items()}
    out = []
    for c in claims:
        item = dict(c)
        quote = normalize(c.get("quote", ""))
        if c.get("malformed"):
            item.update(verified=False, reason="the claim is not made of text, passage and quote")
        elif c["passage"] not in normalized:
            item.update(verified=False, reason="the claim names a passage that was not given")
        elif len(quote) < MIN_QUOTE:
            item.update(verified=False, reason="the quote is too short to prove anything")
        elif quote not in normalized[c["passage"]]:
            item.update(verified=False, reason="the quote is not in the passage")
        else:
            item["verified"] = True
        out.append(item)
    return out
