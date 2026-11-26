"""Command line. Every command needs --home or KB_HOME; there is no default data folder.
Commands that change anything take the single-writer lock and first finish or clean up any interrupted commit (commit.recover)."""
import argparse
import json
import os
import sys
import time

from . import __version__, cas, colls, commit, conv_pdf, conv_registry, db, doctor, inbox, inventory, opener, patches, pipeline, relations, review
from .errors import ArtifactsMissing, KbError
from .home import resolve_home
from .search import Searcher
from .writer import WriterLock

DEFAULT_COLLECTION = "default"


def _collection(conn, name, profile=None):
    """The collection called `name`. A profile (visible to agents, may go to an external model) is fixed when the collection is created."""
    row = conn.execute("SELECT collection_id FROM collections WHERE name=?", (name,)).fetchone()
    if row:
        if profile is not None:
            raise KbError(f"the collection {name!r} exists and its profile is fixed; use another --collection name to add with a different profile")
        return row[0]
    return colls.create(conn, name, profile)


def _status(home):
    conn = db.connect(home.db_path, readonly=True)
    try:
        q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]          # noqa: E731
        info = {"schema_version": db.schema_version(conn), "collections": q("SELECT count(*) FROM collections"),
                "sources": q("SELECT count(*) FROM sources"), "blobs": q("SELECT count(*) FROM blobs"),
                "audit_events": q("SELECT count(*) FROM audit_events"),
                "awaiting_conversion": len(pipeline.pending_revisions(conn))}
        info["failed_conversions"] = [
            {"source": sid, "name": name, "reason": json.loads(rep).get("reason") or (json.loads(rep).get("warnings") or ["failed"])[0]}
            for sid, name, rep in conn.execute(
                "SELECT r.source_id, (SELECT value FROM source_locations l WHERE l.source_id = r.source_id AND l.kind='name' ORDER BY seq DESC LIMIT 1), "
                "c.report_json FROM conversions c JOIN revisions r ON r.revision_id = c.revision_id WHERE c.status = 'failed' "
                "AND c.revision_id = (SELECT o.revision_id FROM revision_observations o JOIN revisions r2 ON r2.revision_id=o.revision_id "
                "WHERE r2.source_id = r.source_id ORDER BY o.obs_id DESC LIMIT 1)")]
        info["partial_conversions"] = q("SELECT count(*) FROM conversions WHERE status='partial'")
        info["skipped_at_intake"] = [{"path": p, "reason": r} for p, r in conn.execute("SELECT path, reason FROM intake_skips ORDER BY path")]
        active = commit.active_commit_id(conn)
        info["active_commit"] = active
        if active:
            row = conn.execute("SELECT created_at FROM commits WHERE commit_id=?", (active,)).fetchone()
            info["active_commit_created"] = row[0]
            info["active_chunks"] = q("SELECT count(*) FROM commit_chunks WHERE commit_id=?", active)
        return info
    finally:
        conn.close()


def build_parser():
    p = argparse.ArgumentParser(prog="kb", description="Optional knowledge layer.")
    p.add_argument("--version", action="version", version=f"kb {__version__}")
    p.add_argument("--home", help="data folder (or set KB_HOME); kb has no default location")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the data folder and bring the database to the current schema")
    a = sub.add_parser("add", help="put a file, a folder, pasted text or a URL into the knowledge base")
    a.add_argument("path", nargs="?", help="file or folder")
    a.add_argument("--text", help="paste text instead of a path")
    a.add_argument("--name", help="name for pasted text, e.g. notes.md")
    a.add_argument("--url", help="fetch and snapshot a web page")
    a.add_argument("--allow-private", action="store_true", help="allow a URL that resolves to a private address")
    a.add_argument("--ref", action="store_true", help="keep the original where it is instead of copying it")
    a.add_argument("--settle", type=float, default=1.0, help="seconds a file must have been quiet before it is taken")
    a.add_argument("--collection", default=DEFAULT_COLLECTION)
    a.add_argument("--agent-visible", action="store_true", help="(new collection only) its content may be given to a model by `kb ask`")
    a.add_argument("--allow-external-model", action="store_true", help="(new collection only) its content may go to a model that is not on this machine")
    sub.add_parser("process", help="convert pending sources and derive relations")
    c = sub.add_parser("commit", help="process, then publish a new commit if anything changed")
    c.add_argument("--force", action="store_true")
    c.add_argument("--no-vectors", action="store_true", help="publish a lexical-only commit even though an embedding backend is configured")
    em = sub.add_parser("embed", help="compute the vectors that the next commit needs (slow the first time; safe to stop and run again)")
    em.add_argument("--limit", type=int, help="embed at most this many chunks in this run")
    eb = sub.add_parser("embedder", help="the local embedding backend: pin it, look at it, stop it")
    esub = eb.add_subparsers(dest="ecmd", required=True)
    e1 = esub.add_parser("set", help="pin the llama-server program and the model file (this is where you approve them)")
    e1.add_argument("--server", default=os.environ.get("KB_LLAMA_SERVER"), help="llama-server executable (default: KB_LLAMA_SERVER)")
    e1.add_argument("--model", default=os.environ.get("KB_EMBED_MODEL"), help="embedding model, a .gguf file (default: KB_EMBED_MODEL)")
    e1.add_argument("--instruction", help="query instruction (changing it makes a different index)")
    esub.add_parser("status", help="configuration and whether the resident server is running")
    esub.add_parser("stop", help="stop the resident server")
    op = sub.add_parser("open", help="open a search result at its place (a chunk id from `search --chunks`, or a source with --page)")
    op.add_argument("target", help="chunk id, source id or file name")
    op.add_argument("--page", type=int, help="page of a PDF (when the target is a source)")
    op.add_argument("--launch", action="store_true", help="hand the file URL to the default browser/viewer")
    op.add_argument("--json", action="store_true")
    s = sub.add_parser("search", help="search the active commit")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=10)
    s.add_argument("--chunks", action="store_true", help="chunk-level results instead of one hit per source")
    s.add_argument("--candidates", action="store_true", help="also search sources the active commit does not cover yet")
    s.add_argument("--json", action="store_true")
    s.add_argument("--mode", choices=["auto", "lexical", "dense", "hybrid"], default="auto",
                   help="auto: lexical plus semantic when the commit has vectors from the configured backend, otherwise lexical (and it says so)")
    q = sub.add_parser("ask", help="answer a question from the active commit through a harness process (retrieval first, the model has no tools)")
    q.add_argument("question")
    q.add_argument("--base-url", default=os.environ.get("HARNESS_BASE_URL", ""), help="the model endpoint (or HARNESS_BASE_URL)")
    q.add_argument("--model", default=os.environ.get("HARNESS_MODEL", ""), help="the model name (or HARNESS_MODEL)")
    q.add_argument("--provider", default=os.environ.get("HARNESS_PROVIDER", "vllm"))
    q.add_argument("--harness", default=None, help="path of harness_vllm.py (default: the roy_harness checkout)")
    q.add_argument("--api-key-env", default="", help="NAME of an environment variable that holds the endpoint key (the value is never printed)")
    q.add_argument("--allow-http", action="store_true", help="allow a plain http endpoint that is not on this machine")
    q.add_argument("-k", type=int, default=6, help="how many passages to give the model")
    q.add_argument("--mode", choices=["auto", "lexical", "dense", "hybrid"], default="auto")
    q.add_argument("--timeout", type=float, default=300.0)
    q.add_argument("--withhold-unverified", action="store_true", help="show no answer text when a quote could not be checked")
    q.add_argument("--json", action="store_true")
    co = sub.add_parser("collection", help="collections and who may receive their content: list, or set (agent-visible, allow-external-model)")
    co.add_argument("ccmd", choices=["list", "set"])
    co.add_argument("name", nargs="?")
    co.add_argument("--agent-visible", choices=["yes", "no"], help="may `kb ask` give this collection's passages to a model")
    co.add_argument("--allow-external-model", choices=["yes", "no"], help="may they go to a model that is not on this machine")
    an = sub.add_parser("answers", help="answers that `kb ask` recorded: list, show, release a held one")
    an.add_argument("acmd", choices=["list", "show", "release"])
    an.add_argument("ask_id", nargs="?")
    an.add_argument("--by", help="who looked at it (release)")
    an.add_argument("--json", action="store_true")
    sub.add_parser("status", help="counts, failures, skipped paths, the active commit")
    sub.add_parser("inventory", help="every class of file kb keeps")
    sub.add_parser("rollback", help="return to the previous commit if its files verify")
    sub.add_parser("recover", help="finish or clean up an interrupted commit, then remove unused folders")
    d = sub.add_parser("doctor", help="capabilities and data-folder health")
    d.add_argument("--deep", action="store_true", help="re-hash every blob and re-verify the active commit")
    d.add_argument("--json", action="store_true")
    ln = sub.add_parser("links", help="sources linked to and from a source")
    ln.add_argument("source", help="source id or file name")
    sh = sub.add_parser("show", help="what kb knows about one source")
    sh.add_argument("source", help="source id or file name")
    sh.add_argument("--include-hidden", action="store_true", help="also show a source hidden at level b")
    dl = sub.add_parser("delete", help="hide a source or unit (level a: not searchable; level b: also hidden from general access). The original file stays")
    dl.add_argument("target", help="source id, file name or unit id")
    dl.add_argument("--level", choices=["a", "b"], default="a")
    dl.add_argument("--by", required=True, help="who is asking")
    dl.add_argument("--yes", action="store_true", help="also approve and apply it now, as the same person")
    pt = sub.add_parser("patch", help="correct the text of one unit (a patch: the converted output and the original stay untouched)")
    pt.add_argument("unit_version")
    pt.add_argument("--set-text", help="replace the whole unit text")
    pt.add_argument("--find", help="text to replace (must occur exactly once)")
    pt.add_argument("--replace", help="its replacement")
    pt.add_argument("--by", required=True)
    pt.add_argument("--yes", action="store_true", help="also approve and apply it now, as the same person")
    rv = sub.add_parser("review", help="proposals: list, show, approve, reject, resolve a patch conflict, purge old differences")
    rsub = rv.add_subparsers(dest="rcmd", required=True)
    rsub.add_parser("list", help="pending proposals and patches that need a decision")
    r1 = rsub.add_parser("show", help="what a proposal would change")
    r1.add_argument("proposal")
    r2 = rsub.add_parser("approve", help="approve and apply a proposal (visible after the next commit)")
    r2.add_argument("proposal")
    r2.add_argument("--by", required=True)
    r2.add_argument("--override-protected", action="store_true", help="an AI edit may replace human-written content (you saw the difference)")
    r2.add_argument("--acknowledge-unsupported", action="store_true", help="approve although the support check flagged a claim")
    r3 = rsub.add_parser("reject", help="reject a pending proposal")
    r3.add_argument("proposal")
    r3.add_argument("--by", required=True)
    r4 = rsub.add_parser("resolve", help="decide a patch that replay could not place by itself")
    r4.add_argument("patch")
    r4.add_argument("--accept", action="store_true", help="apply it to the unit replay suggests (default: drop it)")
    r4.add_argument("--by", required=True)
    r5 = rsub.add_parser("purge", help="blank the differences of decided proposals older than the retention period")
    r5.add_argument("--days", type=int, default=review.RETENTION_DAYS)
    return p


def _find_source(conn, ident):
    if conn.execute("SELECT 1 FROM sources WHERE source_id=?", (ident,)).fetchone():
        return ident
    rows = conn.execute("SELECT DISTINCT source_id FROM source_locations WHERE kind='name' AND lower(value)=lower(?)", (ident,)).fetchall()
    if len(rows) != 1:
        raise KbError(f"{ident!r} matches {len(rows)} sources; use a source id")
    return rows[0][0]


def _general_access(conn, sid, ident, include_hidden=False):
    """Level b means hidden from general access: every command that names a source says what it says for a name that never existed."""
    if commit.tombstone_level(conn, sid) == "b" and not include_hidden:
        raise KbError(f"{ident!r} matches 0 sources; use a source id")


def _show(conn, args, out):
    sid = _find_source(conn, args.source)
    level = commit.tombstone_level(conn, sid)
    _general_access(conn, sid, args.source, args.include_hidden)
    head = commit.statement_head(conn, sid, "title")
    title = json.loads(conn.execute("SELECT value_json FROM statements WHERE statement_id=?", (head,)).fetchone()[0]) if head else None
    locs = conn.execute("SELECT kind, value FROM source_locations WHERE source_id=? ORDER BY seq", (sid,)).fetchall()
    active = commit.active_commit_id(conn)
    in_index = bool(active) and conn.execute(
        "SELECT 1 FROM commit_units cu JOIN unit_versions uv ON uv.unit_version_id = cu.unit_version_id JOIN conversions cv ON cv.conv_id = uv.conv_id "
        "JOIN revisions r ON r.revision_id = cv.revision_id WHERE cu.commit_id = ? AND r.source_id = ? LIMIT 1", (active, sid)).fetchone() is not None
    print(f"source {sid}\n  title: {title}", file=out)
    for kind, value in locs:
        print(f"  {kind}: {value}", file=out)
    print(f"  revisions: {conn.execute('SELECT count(*) FROM revisions WHERE source_id=?', (sid,)).fetchone()[0]}", file=out)
    conv = commit.current_conversion(conn, sid)
    if conv:
        rows = conn.execute("SELECT ordinal, unit_version_id FROM conversion_units WHERE conv_id = ? ORDER BY ordinal LIMIT 30", (conv[0],)).fetchall()
        shown = [(o, uv) for o, uv in rows if commit.hidden_level(conn, uv) != "b"]       # general access: a unit hidden at level b is not shown
        print(f"  units (the first {len(shown)}; kb patch UNIT_VERSION corrects one)" + (f"; {len(rows) - len(shown)} hidden" if len(rows) > len(shown) else "") + ":", file=out)
        for ordinal, uv in shown:
            print(f"    {ordinal:3d}  {uv}  {(patches.shown_text(conn, uv) or '')[:60]!r}", file=out)
    print(f"  searchable in the active commit: {'yes' if in_index and not level else 'no'}", file=out)
    if level:
        print(f"  HIDDEN at level {level} ({'not searchable' if level == 'a' else 'not searchable and hidden from general access'}); the original file is kept", file=out)
    return 0


def _hit_line(i, h, out):
    flags = "" if h.status == "ok" else f"  [{h.status}]"
    flags += "  [newer revision not indexed yet]" if h.outdated else ""
    tag = "  (unprocessed candidate)" if h.state == "candidate" else ""
    page = f"  page {h.locator['page']}" if h.locator.get("page") else ""
    print(f"{i:2d}. {h.score:7.3f}  {h.title}  ({h.name}){page}{tag}{flags}", file=out)
    print(f"      {h.location}", file=out)
    print(f"      {h.snippet}", file=out)
    if h.why:
        print(f"      matched: {', '.join(h.why)}" + (f"   also in {len(h.also_in)} other source(s)" if h.also_in else ""), file=out)


def _embedder(home):
    """The configured embedding backend, or None when none is pinned. Imported here so that kb without numpy still runs everything else."""
    from . import embedder
    return embedder.LlamaServerEmbedder(home) if embedder.load_config(home) else None


def _extensions(home, args):
    if getattr(args, "no_vectors", False):
        return []
    emb = _embedder(home)
    if emb is None:
        return []
    from .vecindex import VectorExtension
    return [VectorExtension(home, emb)]


def _embed_command(home, args, out):
    from . import vecindex
    emb = _embedder(home)
    if emb is None:
        raise KbError("no embedding backend is configured; run kb embedder set --server <llama-server> --model <gguf>")
    with WriterLock(home.path):                      # only the short preparation holds the lock; the long embedding does not
        conn = db.connect(home.db_path)
        try:
            for cid, action in commit.recover(home, conn):
                print(f"recovered commit {cid}: {action}", file=out)
            pipeline.process_pending(conn, cas.BlobStore(home.sub("blobs")))
            relations.derive(conn)
            patches.replay(conn)
            asm = commit.assemble(conn)
        finally:
            conn.close()
    identity = emb.identity
    cache = vecindex.VectorCache(vecindex.cache_path(home))
    try:
        todo = vecindex.missing(cache, identity, asm)
        total = len(todo)
        if args.limit is not None:
            todo = dict(sorted(todo.items())[:args.limit])
        print(f"{len(vecindex.chunk_shas(asm))} distinct chunk texts; {total} without a vector; embedding {len(todo)}", file=out)
        t0 = time.monotonic()
        last = [0.0]

        def progress(done, n):
            if time.monotonic() - last[0] > 20 or done == n:
                last[0] = time.monotonic()
                print(f"  {done}/{n} ({done / max(time.monotonic() - t0, 1e-9):.1f} per second)", file=out, flush=True)
        vecindex.embed_missing(cache, emb, identity, todo, progress)
        print(f"{total - len(todo)} still without a vector" if total > len(todo) else "every chunk has its vector; run kb commit", file=out)
    finally:
        cache.close()
    return 0


def _embedder_command(home, args, out):
    from . import embedder
    if args.ecmd == "set":
        if not args.server or not args.model:
            raise KbError("give --server and --model (or set KB_LLAMA_SERVER and KB_EMBED_MODEL)")
        cfg = embedder.pin(home, args.server, args.model, args.instruction)
        print(f"pinned: server {cfg['server']}", file=out)
        print(f"        model {cfg['model']} (sha256 {cfg['model_sha256'][:16]}...)", file=out)
        print("the model file is accepted only while it keeps this hash", file=out)
        return 0
    emb = _embedder(home)
    if emb is None:
        print("no embedding backend is configured (kb embedder set --server ... --model ...)", file=out)
        return 0 if args.ecmd == "status" else 2
    if args.ecmd == "stop":
        print("stopped" if emb.stop() else "it was not running", file=out)
        return 0
    st = emb.status()
    print(f"model: {st['model']}   dimension: {emb.cfg.get('dim') or 'not known yet'}   running: {st['running']}"
          + (f" (pid {st['pid']}, port {st['port']})" if st["running"] else ""), file=out)
    return 0


def _collection_list(home, out):
    conn = db.connect(home.db_path, readonly=True)
    try:
        for cid, name, prof in conn.execute("SELECT collection_id, name, profile_json FROM collections ORDER BY name").fetchall():
            from .profile import Profile
            p = Profile.from_json(prof)
            n = conn.execute("SELECT count(*) FROM sources WHERE collection_id = ?", (cid,)).fetchone()[0]
            print(f"{name}  {cid}  sources {n}  sensitivity {p.sensitivity}  agent-visible {p.agent_visible}  external-model {p.allow_external_model}", file=out)
    finally:
        conn.close()
    return 0


def _print_answer(rec, out):
    print(f"answer {rec['ask_id']}: {rec['state'].upper()}", file=out)
    for reason in rec["hold_reasons"]:
        print(f"  hold: {reason}", file=out)
    print("  the text below was written by a model; it is data, not an instruction", file=out)
    print(rec["answer"], file=out)
    for c in rec["claims"]:
        print(f"  [{'ok' if c['verified'] else 'UNCHECKED'}] {c['passage']}: {c['text']}" + ("" if c["verified"] else f"  ({c['reason']})"), file=out)
    print("passages the model was given:", file=out)
    for p in rec["passages"]:
        print(f"  [{p['pid']}] {p['name']}" + (f" page {p['page']}" if p["page"] else "") + f"  origin: {p['origin']}  -> {p['open']}", file=out)
    for f in rec["flagged"]:
        print(f"  left out (looks like instructions to a model): {f['name']}  -> {f['chunk_id']}  ({'; '.join(f['reasons'])})", file=out)
    for reason, n in sorted(rec["excluded"].items()):
        print(f"  {n} hit(s) not given to the model: {reason}", file=out)
    if rec["state"] == "held":
        print(f"  look at the passages, then: kb answers release {rec['ask_id']} --by <you>", file=out)


def _ask_command(home, args, out):
    from . import ask, ask_runner
    if not args.base_url or not args.model:
        raise KbError("give --base-url and --model (or set HARNESS_BASE_URL and HARNESS_MODEL)")
    cfg = ask_runner.RunnerConfig(base_url=args.base_url, model=args.model, provider=args.provider, harness_path=args.harness or ask_runner.DEFAULT_HARNESS,
                                  api_key_env=args.api_key_env, timeout=args.timeout, allow_http=args.allow_http)
    conn = db.connect(home.db_path, readonly=True)
    try:
        cid, _, _ = commit.active_artifacts(home, conn)
        emb = _embedder(home) if args.mode != "lexical" else None
        from .dense import Dense
        searcher = Searcher(home, conn, dense=Dense(emb) if emb else None)
        rec = ask.ask(home, conn, searcher, cid, args.question, cfg, k=args.k, mode="lexical" if emb is None else args.mode,
                      withhold_unverified=args.withhold_unverified)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(rec, ensure_ascii=False, indent=1), file=out)
    else:
        _print_answer(rec, out)
    return 4 if rec["state"] == "held" else 0


def _answers_command(home, args, out):
    from . import ask_outbox
    outdir = os.path.join(home.path, "ask", "outbox")
    if args.acmd == "list":
        for ask_id in ask_outbox.list_ids(outdir):
            rec = ask_outbox.read(outdir, ask_id)
            state = "released" if rec["released"] else rec["state"]
            print(f"{ask_id}  {state:9s}  {rec['created']}  {rec['question'][:60]!r}", file=out)
        return 0
    if not args.ask_id:
        raise KbError("give the answer id")
    if args.acmd == "show":
        rec = ask_outbox.read(outdir, args.ask_id)
        if args.json:
            print(json.dumps(rec, ensure_ascii=False, indent=1), file=out)
        else:
            _print_answer(rec, out)
            if rec["released"]:
                print("  (released)", file=out)
        return 0
    if not args.by:
        raise KbError("say who looked at it: --by <name>")
    rec = ask_outbox.read(outdir, args.ask_id)
    if rec["state"] != "held":
        print(f"{args.ask_id} was not held; nothing to release", file=out)
        return 0
    print(f"{args.ask_id} released by {args.by}" if ask_outbox.release(outdir, args.ask_id, args.by) else f"{args.ask_id} was already released", file=out)
    return 0


def _open_command(home, args, out):
    conn = db.connect(home.db_path, readonly=True)
    try:
        store = cas.BlobStore(home.sub("blobs"))
        if args.target.startswith("ck_"):
            target = opener.resolve_chunk(home, conn, store, args.target)
        else:
            sid = _find_source(conn, args.target)
            _general_access(conn, sid, args.target)
            target = opener.resolve(home, conn, store, sid, page=args.page)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(target, ensure_ascii=False, indent=1), file=out)
    else:
        print(f"{target['name']}  [{target['status']}; {target['how']}]", file=out)
        print(f"  file: {target['path'] or 'not available'}", file=out)
        if target["page"]:
            print(f"  page: {target['page']}" + (f"   box: {target['bbox']}" if target["bbox"] else ""), file=out)
        print(f"  url:  {target['url'] or 'not offered (' + target['status'] + ')'}", file=out)
    if target["status"] != opener.OK:
        print(f"kb: the file is {target['status'].replace('_', ' ')}; it was not opened", file=sys.stderr)
        return 3
    if args.launch:
        import webbrowser
        webbrowser.open(target["url"])
    return 0


def main(argv=None, out=None):
    out = out or sys.stdout
    conv_registry.register(conv_pdf.PdfConverter())         # optional converters join here (the registry itself may not import them)
    args = build_parser().parse_args(argv)
    try:
        home = resolve_home(args.home)
        cmd = args.cmd
        if cmd == "init":
            applied = home.init()
            print(f"kb home: {home.path}\nschema versions applied: {applied or 'none (already current)'}", file=out)
            return 0
        if cmd == "status":
            info = _status(home)
            for k in ("schema_version", "collections", "sources", "blobs", "audit_events", "awaiting_conversion", "partial_conversions"):
                print(f"{k}: {info[k]}", file=out)
            print(f"active_commit: {info['active_commit'] or 'none'}" + (f"  ({info['active_chunks']} chunks, {info['active_commit_created']})"
                                                                         if info["active_commit"] else ""), file=out)
            print(f"failed conversions: {len(info['failed_conversions'])}", file=out)
            for f in info["failed_conversions"]:
                print(f"  {f['name']}: {f['reason']}", file=out)
            print(f"skipped at intake: {len(info['skipped_at_intake'])}", file=out)
            for s_ in info["skipped_at_intake"]:
                print(f"  {s_['path']}: {s_['reason']}", file=out)
            return 0
        if cmd == "embedder":
            return _embedder_command(home, args, out)
        if cmd == "embed":
            return _embed_command(home, args, out)
        if cmd == "open":
            return _open_command(home, args, out)
        if cmd == "collection" and args.ccmd == "list":
            return _collection_list(home, out)
        if cmd == "ask":
            return _ask_command(home, args, out)
        if cmd == "answers":
            return _answers_command(home, args, out)
        if cmd == "doctor":
            rep = doctor.report(home, deep=args.deep)
            if args.json:
                print(json.dumps(rep, ensure_ascii=False, indent=1), file=out)
            else:
                print("capabilities:", file=out)
                for c in rep["capabilities"]:
                    print(f"  [{'ok' if c['available'] else 'MISSING'}] {c['name']}: {c['detail']}"
                          + ("" if c["available"] else f"  -> affects: {', '.join(c['affects'])}"), file=out)
                if rep["text_key"]["state"] not in ("no_database", "not_applicable"):
                    print(f"text key: {rep['text_key']['state']}: {rep['text_key']['detail']}", file=out)
                print("data folder: " + ("healthy" if rep["healthy"] else "PROBLEMS"), file=out)
                for kind, detail in rep["problems"]:
                    print(f"  {kind}: {detail}", file=out)
            return 0 if rep["healthy"] else 1
        if cmd == "inventory":
            for row in inventory.inventory(home):
                print(f"{row['class']:16s} {row['files']:6d} files {row['bytes']:12d} bytes  {row['path']}", file=out)
            return 0
        if cmd == "search":
            conn = db.connect(home.db_path, readonly=True)
            try:
                emb = _embedder(home) if args.mode != "lexical" else None
                if args.mode in ("dense", "hybrid") and emb is None:
                    raise KbError(f"--mode {args.mode} needs an embedding backend; run kb embedder set --server ... --model ...")
                from .dense import Dense
                s = Searcher(home, conn, dense=Dense(emb) if emb else None)
                mode = "lexical" if emb is None else args.mode
                hits = []
                try:
                    hits = s.chunks(args.query, args.k, mode) if args.chunks else s.sources(args.query, args.k, mode)
                except ArtifactsMissing as e:
                    if not args.candidates:
                        print(f"kb: {e}", file=sys.stderr)
                        return 3
                for note in s.notices:
                    print(f"kb: {note}", file=sys.stderr)
                if args.candidates:
                    hits += s.candidates(args.query, args.k)
                if args.json:
                    print(json.dumps([h.__dict__ for h in hits], ensure_ascii=False, indent=1), file=out)
                else:
                    for i, h in enumerate(hits, 1):
                        _hit_line(i, h, out)
                    if not hits:
                        print("no results", file=out)
                return 0
            finally:
                conn.close()
        if cmd == "links":
            conn = db.connect(home.db_path, readonly=True)
            try:
                sid = _find_source(conn, args.source)
                _general_access(conn, sid, args.source)
                o, i = relations.neighbors(conn, sid)
                name = lambda x: conn.execute("SELECT value FROM source_locations WHERE source_id=? AND kind='name' ORDER BY seq DESC LIMIT 1", (x,)).fetchone()[0]  # noqa: E731
                print(f"{name(sid)} links to {len(o)}:", file=out)
                for x in o:
                    print(f"  {name(x)}  {x}", file=out)
                print(f"linked from {len(i)}:", file=out)
                for x in i:
                    print(f"  {name(x)}  {x}", file=out)
                return 0
            finally:
                conn.close()
        if cmd == "show" or (cmd == "review" and args.rcmd in ("list", "show")):
            conn = db.connect(home.db_path, readonly=True)
            try:
                if cmd == "show":
                    return _show(conn, args, out)
                if args.rcmd == "show":
                    print("\n".join(review.describe(conn, args.proposal)), file=out)
                    return 0
                pend = review.pending(conn)
                print(f"pending proposals: {len(pend)}", file=out)
                for pid in pend:
                    print(review.describe(conn, pid)[0], file=out)
                q = patches.queue(conn)
                print(f"patches that need a decision: {len(q)}", file=out)
                for item in q:
                    print(f"  {item['patch_id']}  replay suggests {item['suggest']}", file=out)
                return 0
            finally:
                conn.close()
        # ---- commands that change things: single writer, recovery first
        with WriterLock(home.path):
            conn = db.connect(home.db_path)
            try:
                for cid, action in commit.recover(home, conn):
                    print(f"recovered commit {cid}: {action}", file=out)
                store = cas.BlobStore(home.sub("blobs"))
                if cmd == "add":
                    wanted = None
                    if args.agent_visible or args.allow_external_model:
                        from .profile import Profile
                        wanted = Profile(agent_visible=args.agent_visible, allow_external_model=args.allow_external_model)
                    cid = _collection(conn, args.collection, wanted)
                    intake = inbox.Intake(conn, store, cid, settle_s=args.settle)
                    mode = "ref" if args.ref else "copy"
                    if args.url:
                        results = [intake.add_url(args.url, allow_private=args.allow_private)]
                    elif args.text is not None:
                        results = [intake.add_text(args.text, args.name)]
                    elif args.path and os.path.isdir(args.path):
                        results = intake.add_folder(args.path, mode)
                    elif args.path:
                        results = [intake.add_file(args.path, mode)]
                    else:
                        raise KbError("give a path, --text or --url")
                    counts = {}
                    for r in results:
                        counts[r.outcome] = counts.get(r.outcome, 0) + 1
                        if r.outcome == "skipped":
                            print(f"skipped {r.path}: {r.reason}", file=out)
                    print("added: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) if counts else "nothing to add", file=out)
                    print("these are candidates until the next `kb commit`", file=out)
                    return 0
                if cmd in ("process", "commit"):
                    done = pipeline.process_pending(conn, store)
                    rel = relations.derive(conn)
                    rep = patches.replay(conn)
                    print(f"converted {len(done)} revision(s): " + ", ".join(f"{k} {sum(1 for d in done if d.status == k)}" for k in ("ok", "partial", "failed")), file=out)
                    print(f"relations: {rel['resolved']} resolved, {rel['unresolved']} unresolved, {rel['ambiguous']} ambiguous", file=out)
                    print(f"patches: {rep['present']} in place, {rep['moved']} followed their unit, {rep['needs_review']} need a decision, "
                          f"{rep['orphaned']} orphaned", file=out)
                    if cmd == "commit":
                        exts = _extensions(home, args)
                        if exts:
                            print(f"vectors: {exts[0].identity.model_id}, {exts[0].identity.dim} dimensions", file=out)
                        r = commit.build(home, conn, force=args.force, extensions=exts)
                        print(f"commit {r.commit_id}: {r.outcome} ({r.chunks} chunks from {r.sources} sources; "
                              f"{r.chunked} chunked, {r.reused} reused)" + (f"; removed {r.pruned} older version(s) that still held hidden content" if r.pruned else ""), file=out)
                        for line in r.erasure_pending:
                            print(f"WARNING: the commit is published, but the erasure of hidden text is not finished: {line}; the next commit tries again", file=out)
                        return 5 if r.erasure_pending else 0
                    return 0
                if cmd == "collection":
                    if not args.name or (args.agent_visible is None and args.allow_external_model is None):
                        raise KbError("give the collection name and --agent-visible and/or --allow-external-model")
                    yes = {"yes": True, "no": False}
                    _, old, new = colls.set_flags(conn, args.name, yes.get(args.agent_visible), yes.get(args.allow_external_model))
                    if new == old:
                        print(f"{args.name}: already so; nothing changed", file=out)
                    else:
                        print(f"{args.name}: agent visible {old.agent_visible} -> {new.agent_visible}; external model {old.allow_external_model} -> {new.allow_external_model}", file=out)
                        print("takes effect at the next `kb commit`; answers of `kb ask` made from this collection while it was open are removed then if it was closed", file=out)
                    return 0
                if cmd == "delete":
                    target = args.target if args.target.startswith("unit_") else _find_source(conn, args.target)
                    pid = review.request_delete(conn, target, args.level, args.by)
                    if args.yes:
                        review.approve(conn, pid, args.by)
                        s = review.apply(conn, pid)
                        print(f"hidden at level {s['level']}: {s['target_id']} (takes effect at the next `kb commit`; the original file stays)", file=out)
                    else:
                        print(f"proposal {pid} created; nothing is hidden until it is approved (kb review approve {pid} --by NAME)", file=out)
                    return 0
                if cmd == "patch":
                    if args.set_text is not None:
                        diff = {"unit_version_id": args.unit_version, "op": "set_text", "new": args.set_text}
                    elif args.find is not None and args.replace is not None:
                        diff = {"unit_version_id": args.unit_version, "op": "replace", "find": args.find, "replace": args.replace}
                    else:
                        raise KbError("give --set-text, or --find with --replace")
                    pid = review.create(conn, "patch", diff, args.by)
                    if args.yes:
                        review.approve(conn, pid, args.by)
                        review.apply(conn, pid)
                        print(f"patch applied (visible after the next `kb commit`); proposal {pid}", file=out)
                    else:
                        print("\n".join(review.describe(conn, pid)), file=out)
                        print(f"nothing changes until you approve: kb review approve {pid} --by NAME", file=out)
                    return 0
                if cmd == "review":
                    if args.rcmd == "approve":
                        review.approve(conn, args.proposal, args.by, args.override_protected, args.acknowledge_unsupported)
                        print(f"applied: {review.apply(conn, args.proposal)} (visible after the next `kb commit`)", file=out)
                    elif args.rcmd == "reject":
                        review.reject(conn, args.proposal, args.by)
                        print("rejected", file=out)
                    elif args.rcmd == "resolve":
                        new = review.resolve_patch(conn, args.patch, args.accept, args.by)
                        print(f"patch {args.patch}: " + (f"applied to the suggested unit (proposal {new})" if new else "dropped"), file=out)
                    elif args.rcmd == "purge":
                        print(f"blanked the difference of {review.purge_expired(conn, args.days)} decided proposal(s)", file=out)
                    return 0
                if cmd == "rollback":
                    print(f"active commit is now {commit.rollback(home, conn)}", file=out)
                    return 0
                if cmd == "recover":
                    removed = commit.gc(home, conn)
                    print(f"removed unused folders: {removed or 'none'}", file=out)
                    return 0
            finally:
                conn.close()
    except KbError as e:
        print(f"kb: {e}", file=sys.stderr)
        return 2
    return 2
