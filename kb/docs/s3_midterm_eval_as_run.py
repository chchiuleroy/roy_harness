"""S3 midterm, step 2: A (kb, mixed wiki+PDF home, lexical and hybrid) against A0 on the BASELINE sets, once, with the rule frozen in s1/thresholds.json;
and the PDF GAP group (never the acceptance group). Nothing here may be tuned on these results.

The baseline sets were already seen when A0 was built, so they serve only the non-inferiority question; the gap group may guide development (two rounds
at most). The acceptance group is not read: the gap tasks come from their own file (docs/s3_gap_tasks.json, cut out by tools/s3_make_gap_tasks.py),
and this program never opens the frozen file that also holds the acceptance tasks (it only hashes it, to prove the cut file is derived from it).

One attempt, on the record. Before a single task is read, an attempt is appended to docs/s3_midterm_attempts.jsonl; it ends there as `completed` or
`aborted` (with the error). A second attempt is refused unless `--again "<reason>"` is given, and its reason is written down next to the first; results
go to docs/s3_midterm_run<N>.json, which is created exclusively (an earlier result is never overwritten). Dry runs use `--synthetic <dir>`: tasks
that were made up for the purpose, no ledger entry, no result file.

Run: PYTHONUTF8=1 python tools/s3_midterm_eval.py <parent folder of mid_home> [--again "<reason>"] [--synthetic <dir>]"""
import hashlib
import json
import os
import subprocess
import sys
import time
import unicodedata
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))).replace(chr(92), "/")
sys.path.insert(0, ROOT + "/src")
sys.path.insert(0, "C:/Users/roy/roy_km/scripts")

from kb import cas, commit, conv_pdf, conv_registry, db, embedder, opener  # noqa: E402
from kb.dense import Dense  # noqa: E402
from kb.home import Home  # noqa: E402
from kb.search import Searcher  # noqa: E402

S1 = ROOT + "/s1/"
OUT_DIR = ROOT + "/docs"
LEDGER = OUT_DIR + "/s3_midterm_attempts.jsonl"
GAP_FILE = OUT_DIR + "/s3_gap_tasks.json"
COMPARATOR = {"real": "vector", "blind1": "hybrid_rrf", "blind2": "hybrid_page"}        # thresholds.json, O_relevance.text_wiki_noninferiority
COMPARATOR_SCORE = {"real": 25, "blind1": 39, "blind2": 38}                               # the same place: a0_comparator
GAP_N = 30                                                                                 # the PDF gap group of S-1 (thresholds.json, docs/S3_PLAN.md)
S1_LEXICAL_WIKI_ONLY = {"real": 24, "blind1": 36, "blind2": 33}                           # docs/S1_REPORT.md
WARM_UP = ["warm up the server and the page cache", "a paragraph about nothing in particular", "second neutral query for the vector file",
           "unrelated words for timing preparation", "last warm up query before the timed ones"]    # not task queries: a task must never serve as a warm-up


class Abort(Exception):
    pass


def pct(values, p):
    s = sorted(values)
    return round(s[min(len(s) - 1, int(len(s) * p))], 1)


def rss_mb(pid):
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True)
    cell = r.stdout.strip().split('","')[-1].strip('"').replace(" K", "").replace(",", "")
    return round(int(cell) / 1024) if cell.isdigit() else None


def stem(name):
    return os.path.splitext(name)[0]


def sha256_bytes_of(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def ledger():
    if not os.path.isfile(LEDGER):
        return []
    with open(LEDGER, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def ledger_append(rec):
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def query_set(a0_dir, gap_file):
    queries = set()
    for name in COMPARATOR:
        with open(a0_dir + f"{name}.json", encoding="utf-8") as f:
            queries |= {r["query"] for r in json.load(f)["results"]}
    with open(gap_file, encoding="utf-8") as f:
        queries |= {t["query"] for t in json.load(f)["tasks"]}
    return queries


def tasks_digest(tasks):
    return hashlib.sha256(json.dumps(tasks, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def parse(argv):
    opts = {"again": None, "synthetic": None}
    rest = []
    it = iter(argv)
    for a in it:
        if a == "--again":
            opts["again"] = next(it)
        elif a == "--synthetic":
            opts["synthetic"] = next(it).replace(chr(92), "/").rstrip("/")
        else:
            rest.append(a)
    if len(rest) != 1:
        raise SystemExit(__doc__)
    return rest[0], opts


def evaluate(parent, synthetic, run_number):
    import wiki_embed as we
    conv_registry.register(conv_pdf.PdfConverter())
    a0_dir = (synthetic + "/a0/") if synthetic else S1 + "a0/"
    gap_file = (synthetic + "/gap.json") if synthetic else GAP_FILE
    if not synthetic:
        freeze = subprocess.run([sys.executable, S1 + "freeze.py", "--verify"], capture_output=True, text=True, cwd=S1)
        if freeze.returncode != 0:
            raise Abort("the frozen files do not verify: " + (freeze.stdout + freeze.stderr).strip()[-300:])
    if synthetic and query_set(a0_dir, gap_file) & query_set(S1 + "a0/", GAP_FILE):
        raise Abort("a dry run must use made-up tasks: one of its queries is a real task query")
    h = Home(os.path.join(parent, "mid_home"))
    conn = db.connect(h.db_path, readonly=True)
    store = cas.BlobStore(h.sub("blobs"))
    active = commit.active_commit_id(conn)
    manifest, problems = commit.verify_artifacts(commit.segment_dir(h, active))                  # serving checks sizes only; a once-only run checks the hashes
    if problems:
        raise Abort("the active commit's files do not verify: " + "; ".join(problems)[:300])
    emb = embedder.LlamaServerEmbedder(h)
    searcher = Searcher(h, conn, dense=Dense(emb))
    names = [r[0] for r in conn.execute("SELECT value FROM source_locations WHERE kind='name'")]
    stems = [unicodedata.normalize("NFC", stem(os.path.basename(n))).casefold() for n in names]          # the way Windows compares names, not just the exact string
    if len(stems) != len(set(stems)):
        raise Abort("two sources of the home share a name stem: a gold page could be matched by the wrong source")
    present = {os.path.splitext(os.path.basename(p))[0] for p in we.discover_pages()}
    res = {"run_number": run_number, "synthetic": bool(synthetic), "model": emb.identity.as_dict(), "commit": active, "sources_in_home": len(names),
           "active_files_verified_by_hash": True}
    if not synthetic:
        res["frozen_files_verified"] = True
        res["gap_file_sha256"] = sha256_bytes_of(gap_file)
        gap_meta = json.load(open(gap_file, encoding="utf-8"))
        if gap_meta["derived_from_sha256"] != sha256_bytes_of(S1 + "tasks/pdf_tasks.json") or gap_meta["group"] != "gap":
            raise Abort("the gap file is not a cut of the frozen task file")
        if tasks_digest(gap_meta["tasks"]) != gap_meta["tasks_sha256"] or len(gap_meta["tasks"]) != GAP_N:
            raise Abort("the gap tasks are not the ones the cutter wrote")
    for _ in range(3):
        for q in WARM_UP:                                                            # the server, the vector file and the index are warm before anything is timed
            searcher.sources(q, 5, "hybrid")
            searcher.chunks(q, 40, "hybrid")
    lat_source, lat_chunk = [], []

    # ------------------------------------------------------------------ baseline sets: wiki pages, Success@5
    base = {}
    for name, variant in COMPARATOR.items():
        rows = json.load(open(a0_dir + f"{name}.json", encoding="utf-8"))["results"]
        if len({r["id"] for r in rows}) != len(rows):
            raise Abort(f"{name}: task ids are not unique, the per-task results would overwrite each other")
        items = [r for r in rows if not r["gold_missing_from_index"]]                 # the frozen A0 run's own definition of a valid task
        if not synthetic:
            if [r["id"] for r in items] != [r["id"] for r in rows if set(r["gold"]) & present]:
                raise Abort(f"{name}: the tasks whose gold page exists now differ from the frozen set of valid tasks")
        comp_hits = {r["id"]: bool(set(r["gold"]) & set(r[variant][:5])) for r in items}
        if not synthetic and sum(comp_hits.values()) != COMPARATOR_SCORE[name]:
            raise Abort(f"{name}: the comparator scores {sum(comp_hits.values())}, the frozen rule says {COMPARATOR_SCORE[name]}")
        per_mode = {}
        for mode in ("lexical", "hybrid"):
            hits = {}
            for r in items:
                t = time.perf_counter()
                pages = [stem(x.name) for x in searcher.sources(r["query"], 5, mode)]
                if mode == "hybrid":
                    lat_source.append((time.perf_counter() - t) * 1000)
                hits[r["id"]] = bool(set(r["gold"]) & set(pages))
            per_mode[mode] = hits
        entry = {"n_valid": len(items), "comparator_variant": variant, "comparator_score": sum(comp_hits.values())}
        for mode, hits in per_mode.items():
            entry[mode] = {"score": sum(hits.values())}
            cats = {}
            for r in items:
                c = cats.setdefault(r["category"], {"n": 0, "kb": 0, "comparator": 0})
                c["n"] += 1
                c["kb"] += int(hits[r["id"]])
                c["comparator"] += int(comp_hits[r["id"]])
            entry[mode]["by_category"] = cats
            entry[mode]["worst_category_loss"] = max(c["comparator"] - c["kb"] for c in cats.values())
            entry[mode]["lost_queries"] = [r["id"] for r in items if comp_hits[r["id"]] and not hits[r["id"]]]
            entry[mode]["won_queries"] = [r["id"] for r in items if hits[r["id"]] and not comp_hits[r["id"]]]
            entry[mode]["rule_met"] = entry[mode]["score"] >= entry["comparator_score"] - 1 and entry[mode]["worst_category_loss"] <= 1
        entry["s1_lexical_wiki_only_for_reference"] = S1_LEXICAL_WIKI_ONLY[name]
        base[name] = entry
    res["baseline_sets"] = base

    # ------------------------------------------------------------------ PDF gap group (its own file; the acceptance group is not here)
    tasks = json.load(open(gap_file, encoding="utf-8"))["tasks"]
    if not tasks or any(t["group"] != "gap" for t in tasks):
        raise Abort("the gap file holds a task that is not in the gap group")
    pdf = {}
    for mode in ("lexical", "dense", "hybrid"):
        rows = []
        for t in tasks:
            gold = (t["gold"]["pdf"], t["gold"]["page"])
            t0 = time.perf_counter()
            hits = searcher.chunks(t["query"], 40, mode)
            if mode == "hybrid":
                lat_chunk.append((time.perf_counter() - t0) * 1000)
            seen, top = set(), []
            for hit in hits:
                key = (hit.name, hit.locator.get("page"))
                if key not in seen:
                    seen.add(key)
                    top.append((key, hit))
                if len(top) == 5:
                    break
            l1 = any(k[0] == gold[0] for k, _ in top)
            l2_hit = next((hit for k, hit in top if k == gold), None)
            l3 = False
            if l2_hit is not None:
                target = opener.resolve_chunk(h, conn, store, l2_hit.chunk_id)
                l3 = target["status"] == "ok" and (target["name"], target["page"]) == gold and target["url"].endswith(f"#page={gold[1]}")
            rows.append({"id": t["id"], "category": t["category"], "L1": l1, "L2": l2_hit is not None, "L2_at_1": bool(top) and top[0][0] == gold, "L3": l3,
                         "n_results": len(top)})
        cats = {}
        for r in rows:
            c = cats.setdefault(r["category"], {"n": 0, "L1": 0, "L2": 0})
            c["n"] += 1
            c["L1"] += int(r["L1"])
            c["L2"] += int(r["L2"])
        pdf[mode] = {"n": len(rows), "L1": sum(r["L1"] for r in rows), "L2": sum(r["L2"] for r in rows), "L2_at_1": sum(r["L2_at_1"] for r in rows),
                     "L3": sum(r["L3"] for r in rows), "by_category": cats, "per_task": rows}
    res["pdf_gap_group"] = {"kb": pdf, "s1_baseline_system_n30": {"bm25": {"L1": 16, "L2": 15, "L2_at_1": 6}, "dense": {"L1": 28, "L2": 18, "L2_at_1": 14},
                                                                 "hybrid": {"L1": 28, "L2": 19, "L2_at_1": 15}}}

    # ------------------------------------------------------------------ cost and speed (this machine, one run, not a general value)
    embed_ms = []
    for i in range(40):
        t = time.perf_counter()
        emb.embed_query(f"neutral timing probe number {i} about nothing in particular")
        embed_ms.append((time.perf_counter() - t) * 1000)
    res["latency_ms_hybrid_source_view"] = {"n": len(lat_source), "median": pct(lat_source, 0.5), "p95": pct(lat_source, 0.95), "max": round(max(lat_source), 1)}
    res["latency_ms_hybrid_chunk_view"] = {"n": len(lat_chunk), "median": pct(lat_chunk, 0.5), "p95": pct(lat_chunk, 0.95), "max": round(max(lat_chunk), 1)}
    res["latency_ms_query_embedding_alone"] = {"n": len(embed_ms), "median": pct(embed_ms, 0.5), "p95": pct(embed_ms, 0.95)}
    st = emb.status()
    res["embedding_server"] = {"resident_mb": rss_mb(st["pid"]) if st["pid"] else None, "running": st["running"]}
    conn.close()
    return res


def main():
    parent, opts = parse(sys.argv[1:])
    if opts["synthetic"]:
        res = evaluate(parent, opts["synthetic"], 0)
        print("synthetic dry run ok: nothing recorded;", sorted(res), "gap modes", sorted(res["pdf_gap_group"]["kb"]),
              "L3 reached for", {m: d["L3"] for m, d in res["pdf_gap_group"]["kb"].items()})
        return
    done = [r for r in ledger() if r["kind"] == "attempt"]
    n = len(done) + 1
    if done and not opts["again"]:
        sys.exit(f"refused: {len(done)} attempt(s) are already on the record in {LEDGER}; a further one needs --again \"<reason>\", which is written down next to the first")
    token = uuid.uuid4().hex
    ledger_append({"kind": "attempt", "n": n, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "reason": opts["again"], "token": token})       # nothing else is read first
    out = OUT_DIR + f"/s3_midterm_run{n}.json"
    try:
        first = [r for r in ledger() if r["kind"] == "attempt" and r["n"] == n][0]
        if first["token"] != token:                                                  # two processes started together: only the first line proceeds
            raise Abort(f"another process recorded attempt {n} first; this one stops without reading a task")
        ledger_append({"kind": "provenance", "n": n, "script_sha256": sha256_bytes_of(__file__), "gap_file_sha256": sha256_bytes_of(GAP_FILE),
                       "frozen_task_file_sha256": sha256_bytes_of(S1 + "tasks/pdf_tasks.json")})
        res = evaluate(parent, None, n)
        with open(out, "x", encoding="utf-8") as f:                                  # exclusive: an earlier result is never overwritten
            json.dump(res, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
    except BaseException as e:
        ledger_append({"kind": "aborted", "n": n, "token": token, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "error": f"{type(e).__name__}: {str(e)[:500]}"})
        raise
    ledger_append({"kind": "completed", "n": n, "token": token, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "result_file": os.path.basename(out),
                   "result_sha256": sha256_bytes_of(out)})
    print(json.dumps({k: v for k, v in res.items() if k != "pdf_gap_group"}, ensure_ascii=False)[:3500])
    print(json.dumps({m: {k: v for k, v in d.items() if k not in ("per_task", "by_category")} for m, d in res["pdf_gap_group"]["kb"].items()}))


if __name__ == "__main__":
    main()
