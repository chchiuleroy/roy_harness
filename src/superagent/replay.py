"""Replaying a labelled corpus through the model exactly as the orchestrator would, and scoring it.

The replay is not a copy of the model path: it RUNS it. A throw-away home, an `Engine` with
`max_running=0` (no ticket process is ever started), a model session per batch, the real batching,
retries and `unavailable` rule. What is different from a live run is listed in `DIFFERENCES` and
written into every report; the report never claims the two are the same.

Batches are made by us, not by arrival time: a chunk of at most `batch_max` rows is put in the inbox
at once and becomes exactly one batch. Reversing a chunk reverses the order inside the batch and
keeps its members.

Everything the replay writes to disk (the corpus rows, the model session's folders) lives in a
temporary folder that is deleted when the pass ends. A hard crash can leave it behind; its name starts
with `superagent-replay-`.
"""
import dataclasses
import datetime
import hashlib
import json
import math
import os
import platform
import sys
import tempfile
import time

from . import corpus as corpus_mod
from . import executor as ex
from . import main_session as ms
from . import measure
from .engine import Engine
from .gate import Policy, Rule, RuleAssessor
from .measure import NULL, UNAVAILABLE
from .sources import FileInbox, FileSink
from .store import EVAL_MAX_ATTEMPTS, Store

SCHEMA = 1
MAX_REPEATS = 20
UNCERTAIN_WARNING_SHARE = 0.10

DIFFERENCES = (
    "The source is a temporary file inbox, not the real source.",
    "Batches are formed from chunks of the corpus, not from arrival times; with order_check the order inside a chunk is reversed.",
    "Every pass runs in a fresh temporary home and store.",
    "The accept mode is fixed to observe, so no ticket is opened; only the model's classification is measured.",
    "The model, endpoint and sampling settings are the ones given to this run and may differ from a live run.",
    "The labels were written by a person; they are not the decisions a live run recorded.",
)


@dataclasses.dataclass(frozen=True)
class ReplayConfig:
    provider: str
    base_url: str
    model: str
    rules: tuple                       # gate.Rule
    repeats: int = 1
    order_check: bool = False
    batch_max: int = ms.BATCH_MAX
    card_cap: int = ms.CARD_CAP
    half_width: float = 0.10
    confidence: float = 0.95
    chunk_timeout: float = 600.0
    eval_timeout: float = 180.0
    backoff: float = 1.0
    hash_ids: bool = False
    executor_factory: object = None    # label -> executor; tests inject a private harness tree
    harness_path: str = None
    progress: object = None            # callable(str): one-line status; the CLI prints it to stderr

    def validate(self):
        def whole(v, lo, hi, name):
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError(f"{name} must be a whole number from {lo} to {hi}")

        def number(v, name, lo=None, hi=None, hi_open=False):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"{name} must be a finite number")
            if (lo is not None and v <= lo) or (hi is not None and (v >= hi if hi_open else v > hi)):
                raise ValueError(f"{name} is out of range")

        if not self.rules or not all(isinstance(r, Rule) for r in self.rules):
            raise ValueError("at least one rule is needed")
        whole(self.repeats, 1, MAX_REPEATS, "repeats")
        whole(self.batch_max, 1, 100, "batch_max")
        whole(self.card_cap, 1, 100_000, "card_cap")
        number(self.half_width, "half_width", lo=0, hi=0.5)
        number(self.confidence, "confidence", lo=0, hi=1, hi_open=True)
        number(self.chunk_timeout, "chunk_timeout", lo=0)
        number(self.eval_timeout, "eval_timeout", lo=0)
        return self


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def outcome_of(decision, rule_names):
    """A recorded decision -> NULL, UNAVAILABLE or a rule name, and why when it is UNAVAILABLE."""
    if decision["verdict"] == "unavailable":
        return UNAVAILABLE, ("unknown_rule" if decision["reason"] == "unknown rule" else "no_usable_answer")
    mr = decision["model_rule"]
    if mr == "":
        return NULL, None
    if mr in rule_names:
        return mr, None
    return UNAVAILABLE, "no_model_answer"


class _Pass:
    """One throw-away home with an engine that runs batches; restarted if a chunk fails."""

    def __init__(self, cfg, rows):
        self.cfg = cfg
        self.senders = frozenset(r.sender for r in rows)
        self.names = {r.name for r in cfg.rules}
        self.tmp = tempfile.TemporaryDirectory(prefix="superagent-replay-")
        self.n = 0                       # position counter: file names keep the order a chunk is put in
        self.store = self.engine = None
        self._start()

    def _factory(self):
        if self.cfg.executor_factory is not None:
            return self.cfg.executor_factory
        xc = ex.ExecutorConfig(self.cfg.provider, self.cfg.base_url, self.cfg.model,
                               api_key=os.environ.get("SUPERAGENT_API_KEY", ""), backoff=self.cfg.backoff)
        return lambda label: ex.HarnessExecutor(xc)

    def _start(self):
        c = self.cfg
        gen = len(os.listdir(self.tmp.name))
        base = os.path.join(self.tmp.name, f"h{gen}")
        self.inbox = os.path.join(base, "inbox")
        os.makedirs(self.inbox)
        policy = Policy(allowed_senders=self.senders, accept_mode="observe", rules=tuple(c.rules))
        self.store = Store(os.path.join(base, "home")).open()
        main = ms.MainSession(ex.ExecutorConfig(c.provider, c.base_url, c.model, backoff=c.backoff), policy,
                              os.path.join(base, "home"), self._factory(), reset_every=1)
        self.engine = Engine(self.store, FileInbox(self.inbox), FileSink(os.path.join(base, "out")), policy,
                             RuleAssessor(), self._factory(), max_running=0, main=main, batch_max=c.batch_max,
                             batch_min=1, batch_wait=0.0, eval_timeout=c.eval_timeout, card_cap=c.card_cap)

    def _stop(self):
        if self.engine is not None:
            try:
                self.engine.shutdown()
            finally:
                self.engine = None
                if self.store is not None:
                    self.store.close()
                    self.store = None

    def close(self):
        try:
            self._stop()
        finally:
            self.tmp.cleanup()

    def run_chunk(self, rows):
        """-> {row id: (outcome, why or None, failure kind or None)}"""
        want = {}
        for row in rows:
            self.n += 1
            path = os.path.join(self.inbox, f"{self.n:06d}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"id": row.id, "thread": row.thread, "sender": row.sender, "text": row.text,
                           "addressed": False}, f)
            old = time.time() - 10
            os.utime(path, (old, old))
            want[row.id] = row
        started, deadline, failure = time.time(), time.time() + self.cfg.chunk_timeout, None
        got = {}
        while True:
            self.engine.tick()
            got = self._decided(want)
            if len(got) == len(want):
                break
            if any(e["kind"] == "main_cannot_start" and e["ts"] >= started for e in self.engine.events):
                failure = "cannot_start"
                break
            if time.time() > deadline:
                failure = "timeout"
                break
            time.sleep(0.2)
        out = {}
        for rid in want:
            if rid in got:
                oc, why = outcome_of(got[rid], self.names)
                out[rid] = (oc, why, None)
            else:
                out[rid] = (UNAVAILABLE, failure, failure)
        if failure:                                   # whatever was in flight must not leak into the next chunk
            self._stop()
            self._start()
        return out

    def _decided(self, want):
        rows = self.store._q(
            "SELECT t.ext_id AS ext_id, d.verdict AS verdict, d.model_rule AS model_rule, d.reason AS reason "
            "FROM decision d JOIN trigger t ON t.id = d.trigger_id")
        return {r["ext_id"]: r for r in rows if r["ext_id"] in want}


def _say(cfg, text):
    if cfg.progress is not None:
        cfg.progress(text)


def _run_pass(cfg, rows, reverse, label=""):
    """One full pass over the corpus. -> ({row id: outcome}, {row id: why}, {row id: failure}, {row id: chunk size})"""
    outcomes, whys, failures, sizes = {}, {}, {}, {}
    p = _Pass(cfg, rows)
    try:
        chunks = list(corpus_mod.chunks(rows, cfg.batch_max))
        for k, chunk in enumerate(chunks, 1):
            _say(cfg, f"{label}: batch {k}/{len(chunks)}")
            ordered = list(reversed(chunk)) if reverse else chunk
            for rid, (oc, why, fail) in p.run_chunk(ordered).items():
                outcomes[rid], sizes[rid] = oc, len(chunk)
                if why:
                    whys[rid] = why
                if fail:
                    failures[rid] = fail
    finally:
        p.close()
    return outcomes, whys, failures, sizes


def _headline(block):
    return {k: {"k": block[k]["k"], "n": block[k]["n"], "estimate": block[k]["estimate"]}
            for k in ("recall", "precision", "specificity", "availability") if k in block}


def _meta(cfg, corpus, harness_path):
    from .main_session import build_batch_text, router_memory
    probe = [{"trigger_id": 1, "source": "file:inbox", "thread": "t", "sender": "labeler", "text": "probe"}]
    try:
        with open(harness_path, "rb") as f:
            harness_sha = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        harness_sha = None
    return {
        "model": cfg.model, "provider": cfg.provider,
        "rules": [{"name": r.name, "instruction": r.instruction, "sha256": _sha(r.name + "\n" + r.instruction)}
                  for r in cfg.rules],
        "template_fingerprint": _sha(router_memory(cfg.rules) + "\n" + build_batch_text(probe, cfg.rules, cfg.card_cap)),
        "harness_sha256": harness_sha,
        "sampling": "no temperature or top_p is set by this tool: the endpoint default applies",
        "card_cap": cfg.card_cap, "batch_max": cfg.batch_max, "eval_max_attempts": EVAL_MAX_ATTEMPTS,
        "corpus_sha256": corpus.sha256, "corpus_rows": len(corpus.rows),
        "repeats": cfg.repeats, "order_check": cfg.order_check, "confidence": cfg.confidence,
        "half_width": cfg.half_width, "hash_ids": cfg.hash_ids,
        "python": sys.version.split()[0], "platform": platform.platform(),
        "utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def replay(corpus, cfg):
    """Run the corpus through the model and return the report (a dict; no message text, no reasons)."""
    cfg.validate()
    rows = list(corpus.rows)
    names = [r.name for r in cfg.rules]
    total = cfg.repeats + (1 if cfg.order_check else 0)
    forward = [_run_pass(cfg, rows, False, f"pass {i + 1}/{total}") for i in range(cfg.repeats)]
    reversed_pass = (_run_pass(cfg, rows, True, f"pass {total}/{total} (reversed order)")
                     if cfg.order_check else None)

    scored = [i for i, r in enumerate(rows) if not r.uncertain and r.truncated_chars == 0]

    def table(outcomes, idx):
        return [{"label": rows[i].label, "outcome": outcomes[rows[i].id]} for i in idx]

    per_repeat = [measure.score(table(f[0], scored), names, cfg.confidence) for f in forward]
    primary_block = per_repeat[0]
    uncertain_idx = [i for i, r in enumerate(rows) if r.uncertain]
    dist = {}
    for i in uncertain_idx:
        key = forward[0][0][rows[i].id]
        dist[key] = dist.get(key, 0) + 1
    failures = {"timeout": 0, "cannot_start": 0, "unknown_rule": 0, "no_usable_answer": 0, "no_model_answer": 0}
    for _, whys, fails, _ in forward[:1]:
        for rid, w in whys.items():
            failures[w] = failures.get(w, 0) + 1
    warnings = list(corpus.warnings)
    share = len(uncertain_idx) / len(rows) if rows else 0
    if share > UNCERTAIN_WARNING_SHARE:
        warnings.append(f"{len(uncertain_idx)} of {len(rows)} rows are uncertain and are not scored: the main "
                        f"figures describe the rows a labeller could decide, which may be the easier ones")
    order = None
    if reversed_pass is not None:
        sizes = forward[0][3]
        order = measure.order_sensitivity(
            [forward[0][0][rows[i].id] for i in scored], [reversed_pass[0][rows[i].id] for i in scored],
            [sizes[rows[i].id] == 1 for i in scored],
            baseline=[forward[1][0][rows[i].id] for i in scored] if cfg.repeats >= 2 else None,
            confidence=cfg.confidence)

    def rid(r):
        return corpus_mod.hashed_id(r.id) if cfg.hash_ids else r.id

    return {
        "schema": SCHEMA,
        "meta": _meta(cfg, corpus, cfg.harness_path or ex.HARNESS_PATH),
        "corpus": {"rows": len(rows), "scored": len(scored), "positives": sum(1 for i in scored if rows[i].label),
                   "negatives": sum(1 for i in scored if not rows[i].label), "uncertain": len(uncertain_idx),
                   "truncated": sum(1 for r in rows if r.truncated_chars), "shared_thread_rows": corpus.shared_thread_rows},
        "primary": primary_block["primary"],
        "diagnostic": primary_block["diagnostic"],
        "conclusion": measure.precision_of_estimate(primary_block["primary"], cfg.half_width, cfg.confidence),
        "per_repeat": [_headline(s["primary"]) for s in per_repeat],
        "stability": measure.stability([[f[0][rows[i].id] for i in scored] for f in forward]),
        "order_sensitivity": order,
        "uncertain_rows": {"count": len(uncertain_idx), "share": share, "outcomes": dist},
        "failures": failures,
        "warnings": warnings,
        "differences": list(DIFFERENCES),
        "rows": [{"id": rid(r), "label": r.label, "uncertain": r.uncertain, "truncated_chars": r.truncated_chars,
                  "outcomes": [f[0][r.id] for f in forward],
                  "reversed": None if reversed_pass is None else reversed_pass[0][r.id]} for r in rows],
    }
