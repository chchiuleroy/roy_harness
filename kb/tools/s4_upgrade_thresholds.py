"""Measure the converter-upgrade gate (kb.upgrade) on real data instead of proposing numbers (Roy, 2026-11-19: "go do it").

The gate converts a sample of 10 sources with the NEW converter version and lets the upgrade through when the mean distance to the stored output is at most
MAX_MEAN_DISTANCE and no source moves by more than MAX_SINGLE_DISTANCE. What a harmless upgrade looks like was never measured; this tool does it for the
markdown converter on the real wiki (the pages in a home built by tools/s3_midterm_build.py), with six KINDS of change made on purpose:

  harmless (the upgrade should go through)      same output; trailing and doubled spaces removed; curly quotes made straight
  must stop (a person should look first)        every heading path lower-cased; code blocks dropped; tables dropped; paragraphs cut at line breaks

"Harmless" and "must stop" are my judgement of those six changes, stated here so that it can be disputed. For every kind it computes the distance of each of the real
pages, then replays the gate's own sample choice for 200 seeds and counts how often a pair of thresholds lets the upgrade through.
Reads the home read-only and writes nothing but docs/s4_upgrade_thresholds.json.
Run: PYTHONUTF8=1 python tools/s4_upgrade_thresholds.py <parent folder of mid_home>"""
import hashlib
import itertools
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))).replace(chr(92), "/")
sys.path.insert(0, ROOT + "/src")

from kb import cas, commit, db, pipeline, upgrade  # noqa: E402
from kb.conv_base import Unit  # noqa: E402
from kb.conv_markdown import MarkdownConverter  # noqa: E402
from kb.home import Home  # noqa: E402

SEEDS = 200
SAMPLE = int(os.environ.get("KB_UPGRADE_SAMPLE", upgrade.SAMPLE))          # the size of the sample the gate converts


def squeeze(text):
    return "\n".join(" ".join(line.split()) for line in text.splitlines())


def variants():
    def same(units):
        return units

    def tidy(units):
        return [Unit(u.kind, squeeze(u.text), u.locator, u.heading_path) for u in units]

    def straight(units):
        table = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
        return [Unit(u.kind, u.text.translate(table), u.locator, u.heading_path) for u in units]

    def lower_headings(units):
        return [Unit(u.kind, u.text, u.locator, tuple(h.lower() for h in u.heading_path)) for u in units]

    def no_code(units):
        return [u for u in units if u.kind != "code"]

    def no_tables(units):
        return [u for u in units if u.kind != "table"]

    def cut_lines(units):
        out = []
        for u in units:
            parts = [p for p in u.text.split("\n") if p.strip()] if u.kind == "paragraph" else [u.text]
            out.extend(Unit(u.kind, p, u.locator, u.heading_path) for p in parts)
        return out
    return {"same output": (same, True), "spaces tidied": (tidy, True), "curly quotes straightened": (straight, True),
            "heading paths lower-cased": (lower_headings, False), "code blocks dropped": (no_code, False), "tables dropped": (no_tables, False),
            "paragraphs cut at line breaks": (cut_lines, False)}


def main():
    home = Home(os.path.join(sys.argv[1], "mid_home"))
    conn = db.connect(home.db_path, readonly=True)
    store = cas.BlobStore(home.sub("blobs"))
    conv = MarkdownConverter()
    rows = []
    for (sid,) in conn.execute("SELECT source_id FROM sources ORDER BY source_id").fetchall():
        cur = commit.current_conversion(conn, sid)
        if cur and cur[1] == "markdown":
            sha = conn.execute("SELECT blob_sha256 FROM revisions WHERE revision_id = ?", (cur[4],)).fetchone()[0]
            rows.append((sid, sha, pipeline._latest_name(conn, sid), cur[0]))
    print(f"{len(rows)} markdown sources", flush=True)
    fns = variants()
    dist = {k: {} for k in fns}
    loss = {k: {} for k in fns}
    for sid, sha, name, conv_id in rows:
        data = pipeline._read_blob(conn, store, sha)
        pairs = upgrade._current_pairs(conn, conv_id)
        old = [key for key, _ in pairs]
        res = conv.convert(data, {"filename": name})
        for label, (fn, _) in fns.items():
            new_units = fn(res.units)
            dist[label][sid] = upgrade.distance(old, upgrade._unit_keys(new_units))
            loss[label][sid] = upgrade.text_lost(pairs, new_units)
    order = {seed: [sid for sid, *_ in sorted(rows, key=lambda r: hashlib.sha256((str(seed) + r[0]).encode()).hexdigest())][:SAMPLE] for seed in range(SEEDS)}
    result = {"markdown_sources": len(rows), "seeds": SEEDS, "sample": SAMPLE, "variants": {}}
    for label, (_, harmless) in fns.items():
        d = list(dist[label].values())
        per_pair = {}
        for mean_t, single_t in itertools.product((0.01, 0.02, 0.05, 0.10, 0.20), (0.2, 0.3, 0.5, 0.7)):
            ok = 0
            for seed in range(SEEDS):
                s = [dist[label][x] for x in order[seed]]
                ok += int(sum(s) / len(s) <= mean_t and max(s) <= single_t)
            per_pair[f"mean<={mean_t} single<={single_t}"] = ok / SEEDS
        result["variants"][label] = {
            "judged": "harmless" if harmless else "must stop",
            "pages_changed": sum(1 for x in d if x > 0), "mean_distance_all_pages": round(sum(d) / len(d), 4), "max_distance": round(max(d), 3),
            "share_of_pages_above_0.5": round(sum(1 for x in d if x > 0.5) / len(d), 3), "share_of_samples_let_through": per_pair,
            "both_gates_share_of_samples_let_through": sum(1 for seed in range(SEEDS)
                                                          if sum(dist[label][x] for x in order[seed]) / SAMPLE <= 0.05 and max(dist[label][x] for x in order[seed]) <= 0.5
                                                          and max(loss[label][x] for x in order[seed]) <= 0.05) / SEEDS,
            "text_lost_mean": round(sum(loss[label].values()) / len(loss[label]), 4), "text_lost_max": round(max(loss[label].values()), 3),
            "text_lost_share_of_samples_let_through": {f"loss<={t}": sum(1 for seed in range(SEEDS) if max(loss[label][x] for x in order[seed]) <= t) / SEEDS
                                                       for t in (0.0, 0.02, 0.05, 0.10)}}
    # which pair of thresholds separates the judged groups best
    best = []
    for mean_t, single_t in itertools.product((0.01, 0.02, 0.05, 0.10, 0.20), (0.2, 0.3, 0.5, 0.7)):
        key = f"mean<={mean_t} single<={single_t}"
        good = [v["share_of_samples_let_through"][key] for v in result["variants"].values() if v["judged"] == "harmless"]
        bad = [v["share_of_samples_let_through"][key] for v in result["variants"].values() if v["judged"] == "must stop"]
        best.append({"thresholds": key, "harmless_let_through_min": round(min(good), 3), "must_stop_let_through_max": round(max(bad), 3),
                     "harmless_let_through_mean": round(sum(good) / len(good), 3), "must_stop_let_through_mean": round(sum(bad) / len(bad), 3)})
    result["pairs"] = best
    with open(ROOT + f"/docs/s4_upgrade_thresholds_sample{SAMPLE}.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    for label, v in result["variants"].items():
        print(f"{label:32s} {v['judged']:10s} pages changed {v['pages_changed']:4d}/{len(rows)}  mean {v['mean_distance_all_pages']:.3f}  max {v['max_distance']:.2f}  "
              f"let through at (0.05, 0.5): {v['share_of_samples_let_through']['mean<=0.05 single<=0.5']:.2f}  text lost mean {v['text_lost_mean']:.3f} max {v['text_lost_max']:.2f}  "
              f"text-loss gate alone {v['text_lost_share_of_samples_let_through']['loss<=0.05']:.2f}  BOTH gates {v['both_gates_share_of_samples_let_through']:.2f}")


if __name__ == "__main__":
    main()
