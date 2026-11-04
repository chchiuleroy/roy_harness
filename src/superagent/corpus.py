"""The labelled corpus a measurement runs on: one JSON object per line.

  {"id": "m1", "text": "the export fails", "label": "export-bug"}     required: id, text, label
  {"id": "m2", "text": "lunch?", "label": null, "sender": "ann", "thread": "t9",
   "uncertain": false, "note": "free text for the labeller"}           optional: sender, thread, uncertain, note
                                                                       (an export also adds truncated_chars, card_cap)

`label` is a rule name or null (no rule applies). The labeller is a person; this file is the
reference answer, so it is checked hard before anything is sent to a model: a mistake found after an
hour of model calls is an hour lost, and a silent one is a wrong number.
"""
import dataclasses
import hashlib
import json

DEFAULT_SENDER = "labeler"


class CorpusError(ValueError):
    """The corpus cannot be used. `problems` lists every reason found, not just the first."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclasses.dataclass(frozen=True)
class Row:
    id: str
    text: str
    label: "str | None"
    sender: str
    thread: str
    uncertain: bool = False
    note: str = ""
    truncated_chars: int = 0        # how much of the text the model will not see (card_cap)


@dataclasses.dataclass(frozen=True)
class Corpus:
    rows: tuple
    sha256: str
    warnings: tuple
    shared_thread_rows: int         # rows that share a thread with another row (0 unless allowed)


def hashed_id(row_id):
    """The short stable id a report uses under --hash-ids (and the summary uses to match them back)."""
    return hashlib.sha256(row_id.encode("utf-8")).hexdigest()[:12]


def _names(rules):
    return {getattr(r, "name", r) for r in rules}


def load(path, rules, card_cap, allow_shared_threads=False, allow_truncated=False):
    with open(path, "rb") as f:
        raw = f.read()
    names = _names(rules)
    problems, warnings, rows, seen = [], [], [], {}
    cap_differs = 0
    for n, line in enumerate(raw.decode("utf-8-sig").split("\n"), 1):   # not splitlines(): U+2028 etc. may sit inside a JSON string
        if not line.strip():
            continue
        line = line.rstrip("\r")
        where = f"line {n}"
        try:
            obj = json.loads(line)
        except ValueError as e:
            problems.append(f"{where}: not valid JSON ({e})")
            continue
        if not isinstance(obj, dict):
            problems.append(f"{where}: not a JSON object")
            continue
        rid, text = obj.get("id"), obj.get("text")
        if not isinstance(rid, str) or not rid.strip():
            problems.append(f"{where}: id must be a non-empty string")
            rid = f"<line {n}>"
        elif rid in seen:
            problems.append(f"{where}: id {rid!r} already used on line {seen[rid]}")
        else:
            seen[rid] = n
        if not isinstance(text, str) or not text.strip():
            problems.append(f"{where} ({rid}): text must be a non-empty string")
            text = ""
        if "label" not in obj:
            problems.append(f"{where} ({rid}): label is missing (use null for 'no rule applies')")
            label = None
        else:
            label = obj["label"]
            if label is not None and (not isinstance(label, str) or label not in names):
                problems.append(f"{where} ({rid}): label {label!r} is neither null nor one of the rules "
                                f"{sorted(names)}")
                label = None
        sender, thread = obj.get("sender", DEFAULT_SENDER), obj.get("thread", f"t-{rid}")
        for key, val in (("sender", sender), ("thread", thread)):
            if not isinstance(val, str) or not val.strip():
                problems.append(f"{where} ({rid}): {key} must be a non-empty string")
        uncertain = obj.get("uncertain", False)
        if not isinstance(uncertain, bool):
            problems.append(f"{where} ({rid}): uncertain must be true or false")
            uncertain = False
        note = obj.get("note", "")
        # an export (decisions --for-labeling) may already have cut the text and says by how much; that part
        # was never shown to the labeller either, so it counts as cut here too
        exported_cut = obj.get("truncated_chars", 0)
        if isinstance(exported_cut, bool) or not isinstance(exported_cut, int) or exported_cut < 0:
            problems.append(f"{where} ({rid}): truncated_chars must be a whole number, 0 or more")
            exported_cut = 0
        file_cap = obj.get("card_cap")
        if file_cap is not None and file_cap != card_cap:
            cap_differs += 1
        cut = max(0, len(text) + exported_cut - card_cap)
        if cut and not allow_truncated:
            problems.append(f"{where} ({rid}): the text is {len(text)} characters but the model sees {card_cap}; "
                            f"the label would be about text it never read (shorten it, or allow_truncated)")
        rows.append(Row(rid, text, label, sender if isinstance(sender, str) else DEFAULT_SENDER,
                        thread if isinstance(thread, str) else f"t-{rid}", uncertain,
                        note if isinstance(note, str) else "", cut))
    by_thread = {}
    for r in rows:
        by_thread.setdefault(r.thread, []).append(r.id)
    shared = {t: ids for t, ids in by_thread.items() if len(ids) > 1}
    shared_rows = sum(len(ids) for ids in shared.values())
    if shared:
        msg = (f"{shared_rows} rows share a thread with another row ({len(shared)} threads, e.g. "
               f"{sorted(shared)[0]!r}); messages in one thread are not independent, so the intervals are too narrow")
        (warnings if allow_shared_threads else problems).append(msg)
    if cap_differs:
        warnings.append(f"{cap_differs} rows were exported with a card cap different from the {card_cap} used here; "
                        f"the text a labeller saw is not the text the model will see")
    if not rows and not problems:
        problems.append("the corpus has no rows")
    if problems:
        raise CorpusError(problems)
    return Corpus(tuple(rows), hashlib.sha256(raw).hexdigest(), tuple(warnings),
                  shared_rows if allow_shared_threads else 0)


def chunks(rows, batch_max):
    """Consecutive slices of at most batch_max rows. One slice is what goes to the model as one batch."""
    if isinstance(batch_max, bool) or not isinstance(batch_max, int) or batch_max < 1:
        raise ValueError("batch_max must be a whole number, 1 or more")
    rows = list(rows)
    return [rows[i:i + batch_max] for i in range(0, len(rows), batch_max)]
