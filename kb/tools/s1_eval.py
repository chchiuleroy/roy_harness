"""S1 real-data run: (1) index the roy_km wiki with kb and score the three baseline query sets with the same page-level Success@5 and the same
corpus as the old system (wiki_embed.discover_pages), compared with A0's BM25 variant (which sees only the first 400 characters of each chunk, so the "like_for_like" keys below are a reference floor, not a controlled comparison; see docs/S1_REPORT.md); (2) a pilot folder (roy_km/_raw: text, markdown,
html, csv and everything else that kb cannot convert yet) to see what intake and conversion do with real, messy files.
Read-only on Roy's data: everything is copied or referenced into a temporary kb home. Output: docs/s1_wiki_eval.json
Run: PYTHONUTF8=1 python tools/s1_eval.py <temp home parent>"""
import json
import os
import shutil
import stat
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, "C:/Users/roy/roy_km/scripts")

from kb import cas, colls, commit, db, inbox, pipeline, relations  # noqa: E402
from kb.home import Home  # noqa: E402
from kb.search import Searcher  # noqa: E402

SCRIPTS = "C:/Users/roy/roy_km/scripts/"
WIKI = "C:/Users/roy/roy_km/wiki"
RAW = "C:/Users/roy/roy_km/_raw"


def timed(label, fn, log):
    t = time.perf_counter()
    out = fn()
    log[label] = round(time.perf_counter() - t, 2)
    return out


def load_sets():
    sets = {}
    d = json.load(open(SCRIPTS + "eval_queries_real.json", encoding="utf-8"))
    sets["real"] = [(q["query"], set(q["target_pages"])) for q in d["queries"] if q["category"][0] in "1235" or q["id"] == "Q4b-1"]
    for name, f in (("blind1", "eval_queries_blind_v1.json"), ("blind2", "eval_queries_blind_v2.json")):
        sets[name] = [(q["query"], set(q["gold_pages"])) for q in json.load(open(SCRIPTS + f, encoding="utf-8")) if not q.get("skip")]
    return sets


def build_home(parent, name):
    h = Home(os.path.join(parent, name))
    if os.path.isdir(h.path):
        shutil.rmtree(h.path, onexc=lambda fn, path, exc: (os.chmod(path, stat.S_IWRITE), fn(path)))     # blobs are read-only on purpose
    h.init()
    return h


def main():
    parent = sys.argv[1]
    res = {"wiki": {}, "pilot": {}}
    # ------------------------------------------------------------ wiki
    import wiki_embed as we
    files = sorted(we.discover_pages())
    h = build_home(parent, "wiki_home")
    conn = db.connect(h.db_path)
    cid = colls.create(conn, "wiki")
    store = cas.BlobStore(h.sub("blobs"))
    intake = inbox.Intake(conn, store, cid, settle_s=0)
    log = res["wiki"].setdefault("seconds", {})
    outcomes = {}
    for r in timed("add", lambda: [intake.add_file(p) for p in files], log):
        outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
    res["wiki"]["files"], res["wiki"]["intake_outcomes"] = len(files), outcomes
    done = timed("convert", lambda: pipeline.process_pending(conn, store), log)
    res["wiki"]["conversion"] = {k: sum(1 for d in done if d.status == k) for k in ("ok", "partial", "failed")}
    res["wiki"]["relations"] = timed("relations", lambda: relations.derive(conn), log)
    built = timed("commit", lambda: commit.build(h, conn), log)
    res["wiki"]["commit"] = {"chunks": built.chunks, "sources": built.sources,
                             "fts_bytes": os.path.getsize(os.path.join(commit.segment_dir(h, built.commit_id), commit.FTS)),
                             "manifest_bytes": os.path.getsize(os.path.join(commit.segment_dir(h, built.commit_id), commit.MANIFEST))}
    again = timed("commit_again_unchanged", lambda: commit.build(h, conn), log)
    res["wiki"]["second_build"] = again.outcome
    res["wiki"]["ghost_check_problems"] = commit.ghost_check(conn, built.commit_id, commit.segment_dir(h, built.commit_id),
                                                              commit.read_manifest(commit.segment_dir(h, built.commit_id))[0])
    searcher = Searcher(h, conn)
    a0 = json.load(open(ROOT + "/s1/results/a0_summary.json", encoding="utf-8"))
    present = {os.path.splitext(os.path.basename(p))[0] for p in files}
    scores, lat = {}, []
    for name, qs in load_sets().items():
        valid = [(q, g) for q, g in qs if g and g <= present or (g & present)]
        valid = [(q, g) for q, g in qs if g & present]
        hit = 0
        for q, gold in valid:
            t = time.perf_counter()
            pages = [os.path.splitext(x.name)[0] for x in searcher.sources(q, 5)]
            lat.append((time.perf_counter() - t) * 1000)
            hit += int(any(g in pages for g in gold))
        base = a0[name]["success_at_5"]["bm25"]
        scores[name] = {"n_valid": len(valid), "kb_lexical": hit, "a0_bm25": base, "a0_vector": a0[name]["success_at_5"]["vector"],
                        "a0_best_variant": a0[name]["best_score"], "like_for_like_floor": base - 1, "meets_like_for_like": hit >= base - 1}
    res["wiki"]["scores"] = scores
    res["wiki"]["search_ms"] = {"n": len(lat), "median": round(statistics.median(lat), 1), "p95": round(sorted(lat)[int(len(lat) * 0.95)], 1),
                                "max": round(max(lat), 1)}
    conn.close()
    # ------------------------------------------------------------ pilot folder (real messy files)
    h2 = build_home(parent, "pilot_home")
    conn2 = db.connect(h2.db_path)
    cid2 = colls.create(conn2, "raw")
    store2 = cas.BlobStore(h2.sub("blobs"))
    intake2 = inbox.Intake(conn2, store2, cid2, settle_s=0)
    plog = res["pilot"].setdefault("seconds", {})
    results = timed("add_ref", lambda: intake2.add_folder(RAW, "ref"), plog)
    res["pilot"]["intake_outcomes"] = {}
    for r in results:
        res["pilot"]["intake_outcomes"][r.outcome] = res["pilot"]["intake_outcomes"].get(r.outcome, 0) + 1
    done2 = timed("convert", lambda: pipeline.process_pending(conn2, store2), plog)
    res["pilot"]["conversion"] = {k: sum(1 for d in done2 if d.status == k) for k in ("ok", "partial", "failed")}
    reasons = {}
    for d in done2:
        if d.status == "failed":
            reasons[d.reason] = reasons.get(d.reason, 0) + 1
    res["pilot"]["failed_reasons"] = reasons
    res["pilot"]["skips"] = [list(r) for r in conn2.execute("SELECT reason, count(*) FROM intake_skips GROUP BY reason")]
    pb = timed("commit", lambda: commit.build(h2, conn2), plog)
    res["pilot"]["commit"] = {"chunks": pb.chunks, "sources": pb.sources}
    res["pilot"]["blobs_copied_bytes"] = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(h2.sub("blobs")) for f in fs)
    conn2.close()
    out = os.path.join(ROOT, "docs", "s1_wiki_eval.json")
    json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False))


main()
