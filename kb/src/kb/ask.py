"""Answer one question from the knowledge base through a harness child process (stage S4; the overlay layer, modelled on superagent).

Order of work, every step deterministic code except the one model turn:
  1. retrieve with kb's own rules (ask_retrieve): committed content only; hidden, tombstoned and agent-invisible things never reach the prompt;
     passages are labelled by origin and screened, flagged ones are left out;
  2. frame everything outside as quoted untrusted data (ask_frame);
  3. run ONE harness turn in a ticket folder whose hook vetoes every tool (ask_runner);
  4. check every claim's quote against the passage it names (ask_verify);
  5. record the answer, the claims with their verdicts, and THE PASSAGES THE MODEL WAS GIVEN, written by this layer and not by the model, in the outbox;
  6. an answer is `held` until a person releases it when anything weakens it: an unchecked quote, a passage that came from a url or a paste, a passage that
     was flagged and left out, a reply that is not an answer in the agreed form (defence steps 1 to 4 of docs/S4_PLAN.md).
What no step here can prevent: a model that has read a hostile passage from a source the person approved may write a true quote used misleadingly. The
passages beside the answer, and the hold, are what keep that visible.
"""
import os
import secrets
import shutil
import sys
import time

from . import ask_frame, ask_outbox, ask_retrieve, ask_runner, ask_verify, commit
from . import ask_sanitize as sanitize
from .errors import KbError

ANSWER_CAP = 4000
QUESTION_CAP = 2000


def new_ask_id():
    return "ask_" + secrets.token_hex(8)


def _field(passage, name):
    """A retrieved passage is an object, a flagged one is a dict."""
    return passage[name] if isinstance(passage, dict) else getattr(passage, name)


def _is_local(url):
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1")


def ask(home, conn, searcher, commit_id, question, cfg, k=6, mode="auto", withhold_unverified=False, run=None, ask_id=None):
    """Returns the outbox record (a dict). `run` is the runner function (a test replaces it); by default the real harness child."""
    question = sanitize.clean(question).strip()
    if not question:
        raise KbError("the question is empty")
    run = run or ask_runner.run_once
    ask_id = ask_outbox.check_id(ask_id) if ask_id else new_ask_id()          # before any folder is made from it
    base = os.path.join(home.path, "ask")
    ticket = os.path.join(base, "tickets", ask_id)
    work = os.path.join(ticket, "work")
    os.makedirs(work, exist_ok=True)
    try:
        return _answer(conn, searcher, commit_id, question, cfg, k, mode, withhold_unverified, run, ask_id, os.path.join(base, "outbox"), ticket, work)
    finally:
        _remove_ticket(ticket)                        # the session file holds the whole prompt, that is copies of passages: the outbox record is the one record kept


STALE_SUFFIX = commit.STALE_SUFFIX


def _remove_ticket(ticket):
    """Removes the working folder. A folder that is still there (a file held open) gets a marker next to it: the question is over, so `kb commit` removes
    that folder at its next run whatever its age, and it is said out loud (seventh review: `ignore_errors` alone left the person unaware; eighth review:
    the age and hide rules alone do not remove a fresh folder, and a broken stderr must not replace the error of the question this runs after)."""
    for attempt in range(3):
        shutil.rmtree(ticket, ignore_errors=True)
        if not os.path.exists(ticket):
            return
        time.sleep(0.2)
    try:
        with open(ticket + STALE_SUFFIX, "w", encoding="utf-8"):
            pass
    except OSError:
        pass
    try:
        print(f"WARNING: the working folder of this question could not be removed ({ticket}); it holds copies of the passages that were sent. "
              "The next `kb commit` removes it once nothing holds it open.", file=sys.stderr)
    except (OSError, ValueError):                     # a closed or broken stderr must not replace the error of the question
        pass


def _answer(conn, searcher, commit_id, question, cfg, k, mode, withhold_unverified, run, ask_id, outdir, ticket, work):
    got = ask_retrieve.retrieve(conn, searcher, commit_id, question, k=k, mode=mode, external_endpoint=not _is_local(cfg.base_url))
    not_sent = {}
    for p in got.passages:                                   # ONE version of every passage: what is framed, what is checked, what is listed
        sent = ask_frame.canonical(p.text)
        not_sent[p.pid] = len(sanitize.clean(p.text)) - len(sent)
        p.text = sent
    message, cut = ask_frame.build_message(ask_id, commit_id, question, got.passages)
    result = run(cfg, work, os.path.join(ticket, "session.jsonl"), message)

    hold = []
    answer, claims, problem = (None, [], None)
    if result.text and result.text.strip():
        answer, claims, problem = ask_verify.parse_answer(result.text)
    shown = answer if answer is not None else (result.text or "")
    if problem:
        hold.append(problem)
    verdicts = ask_verify.verify(claims, {p.pid: p.text for p in got.passages})
    bad = sum(1 for c in verdicts if not c["verified"])
    if bad:
        hold.append(f"{bad} of {len(verdicts)} claims have a quote that could not be checked against its passage")
    if answer is None and result.text:
        hold.append("the reply is not an answer in the agreed form; it is shown as the model wrote it")
    has_text = bool(result.text and result.text.strip())
    if not has_text:
        hold.append("the model gave no reply" + (" (the call failed)" if result.failure else " (timed out)" if result.timed_out else ""))
    else:
        if answer is not None and not verdicts:
            hold.append("the answer carries no claim that could be checked against a passage")
        if result.timed_out:
            hold.append("the model's turn did not finish in time; the text may be incomplete")
        if result.exited is not None:
            hold.append(f"the harness process ended before the turn did (exit code {result.exited})")
        if result.failure:
            hold.append("the harness reported a failed model call during the turn")
    outside = sorted({p.origin for p in got.passages if p.origin in ask_retrieve.OUTSIDE})
    if outside:
        hold.append("a passage came from outside the person's own files (" + ", ".join(outside) + ")")
    if got.flagged:
        hold.append(f"{len(got.flagged)} passage(s) looked like instructions to a model and were left out of the prompt")
    if result.asked:
        hold.append(f"the model asked for approval {result.asked} time(s); all were refused")
    if withhold_unverified and bad:
        shown = "(withheld: some claims could not be checked; see the claims and the passages)"

    # The model's turn can take minutes; a commit that hid something in the meantime has already cleaned the outbox, so a record made now would bring the
    # text back (sixth review). Look again, with the database as it is now, before anything is written.
    gone = [p for p in list(got.passages) + list(got.flagged) if commit.passage_hidden(conn, {"source_id": _field(p, "source_id"), "chunk_id": _field(p, "chunk_id")})]
    if gone:
        raise KbError(f"something this answer was made from was hidden while the question ran ({len(gone)} passage(s)); nothing was recorded")
    cleaned = sanitize.clean(shown)
    record = {
        "kind": "answer", "ask_id": ask_id, "msg_id": "ans-" + ask_id, "untrusted": True, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "state": "held" if hold else "ready", "hold_reasons": hold,
        "question": question[:QUESTION_CAP], "answer": cleaned[:ANSWER_CAP], "answer_chars_cut": max(0, len(cleaned) - ANSWER_CAP),
        "claims": [{"text": sanitize.clean(c.get("text", ""))[:500], "passage": sanitize.clean_line(c.get("passage", ""))[:12],
                    "quote": sanitize.clean(c.get("quote", ""))[:500], "verified": c["verified"], "reason": c.get("reason")} for c in verdicts],
        "passages": [{"pid": p.pid, "chunk_id": p.chunk_id, "source_id": p.source_id, "name": sanitize.clean_line(p.name)[:200], "page": p.page,
                      "origin": p.origin, "status": p.status, "open": f"kb open {p.chunk_id}", "text": p.text, "chars_not_sent": not_sent[p.pid]}
                     for p in got.passages],
        "flagged": [dict(f, name=sanitize.clean_line(f["name"])[:200]) for f in got.flagged],
        "excluded": got.excluded, "commit_id": commit_id, "framing_cut": cut,
        "run": {"model": cfg.model, "failure": sanitize.clean_line(result.failure or "")[:300] or None, "timed_out": result.timed_out,
                "exited": result.exited, "events": result.events},
        "screening_note": "passages are screened with a short list of patterns; this catches clumsy attempts, not careful ones",
    }
    ask_outbox.write_message(outdir, record)
    return record
