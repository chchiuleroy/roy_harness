# S3 report (2026-11-17 to 18): PDF, dense retrieval, hybrid ranking, `kb open`, midterm judgement

Evidence grades: **measured** = there is output that can be read; **inferred** = a reason, not run; **guess** = neither.
All numbers come from one machine (Windows 11, Python 3.13.7, one GPU llama-server) and one run unless stated otherwise.

## What S3 delivers
* **PDF converter** (`conv_pdf.py`, tier T2): text blocks with page and box, chunks never span pages, encrypted / unreadable / no-text-layer files are visible rows.
* **Embedding backend** (`embedder.py`, T1): one resident `llama-server` on 127.0.0.1, model pinned by hash, fingerprint of the running server, ownership checked before any stop.
* **Vectors as a second artifact of the same commit** (`vecindex.py`, `dense.py`, T1): `vec.npy`, one unit row per member chunk; a different model / dimension / pooling / instruction is refused (T-I2).
* **Search modes** `lexical | dense | hybrid | auto` (RRF, k = 60); `auto` falls back to lexical and says so; explicit dense / hybrid refuse when there are no usable vectors.
* **`kb open`** to file and page (`#page=N`), pinned to the revision the hit came from; moved or changed originals are reported, not opened.
* New commands: `kb embedder set|status|stop`, `kb embed`, `kb open`, `kb search --mode`, `kb commit --no-vectors`.
* One schema migration (5): an index that the search-hit position lookup needs (see "A cost found by the midterm" below).

## Completion conditions (docs/S3_PLAN.md) against results
| # | Condition | Result |
|---|---|---|
| C1 | PDF conversion with page and box; failures are rows | **Met** (measured). Real corpus: 222 PDFs, 8,072 pages: 220 ok, 2 partial, 0 failed; 18 pages without a text layer. `tests/test_pdf.py`. |
| C2 | T-I2 refuse vectors of another identity | **Met** (measured: tests). |
| C3 | T-S1 same members in both indexes; hidden things in neither | **Met** (measured: tests, ghost check on the vector artifact). |
| C4 | T-H6 hit opens to file and page; moved/changed original reported | **Met for the target handed to the viewer** (measured: tests; on the gap group L3 equals L2 in all three modes: lexical 8, dense 16, hybrid 12). Where a viewer finally lands is not checked (needs `#page=N` support). |
| C5 | Hiding leaves no opened copy and no cached vector after the next commit | **Met** (measured: tests; the clean-up is repeated by every later commit, also an unchanged one). |
| C6 | Backend pinned, 127.0.0.1 only, one slot, memory reported | **Met** (tests, flags). Resident memory: **one reading of 73 MB** (Windows working set of the server process right after the run; the weights are on the GPU, so this is **not** a peak and not comparable to the 0.78 GB of S-1). |
| C7 | Every test has a mutant that turns it red; all earlier mutants still killed | **Met** (measured; `docs/s3_full_mutation_run.txt`, `docs/s3_rerun_after_survivors.txt`). Full set of 401 mutants (S0 to S3) on the final code: 394 killed, **7 survived**, 0 could not be applied; 3 that timed out (the machine was busy or asleep) were re-run alone and killed. The survivors were real gaps: the page range of `kb open` was untested against a newer revision; the evaluator's gap-file digest, count, group and active-file hash checks were untested; a changed server flag was not tested; the netstat parser was not tested against a client row; and one condition (`stop` required a health answer) was redundant and wrong for a hung server, so it was removed. After the tests were added, all 51 mutants of the three edited files were re-run: **51 killed, 0 survived, 0 could not apply**. Registry now 402 mutants (S0 82, S1 116, S2 91, S3 113), 561 tests (1 skipped). |
| C8 | Midterm once, frozen rule, descriptive | **Done once** (ledger: attempt 1 completed; `docs/s3_midterm_run1.json`). Result below: **the rule is not met on one of three sets.** |
| C9 | Review by 小o before the numbers are written down | **Done**, two rounds, before the run: `docs/S3_REVIEW.md`. |

## The midterm (measured, once; hybrid mode is the product mode, lexical shown for reference)
Mixed home: 882 wiki pages + 222 PDFs = 1,104 sources (S1 measured 878 wiki pages; the wiki grew by a few), 47,694 chunks, 46,456 distinct texts, one commit with vectors (fts 64.3 MB, manifest 39.0 MB, vec.npy 195.4 MB).
Rule (frozen in S-1): kb Success@5 ≥ comparator − 1 on each set **and** no query category loses more than 1 query against the comparator.

| Set (valid tasks) | A0 comparator | kb hybrid (worst category loss) | rule | kb lexical (worst category loss) | S1 lexical, wiki only |
|---|---|---|---|---|---|
| real (27) | vector 25 | **26** (0) | met | 24 (1), rule met | 24 |
| blind_v1 (40) | hybrid_rrf 39 | **37** (2; lost E3, E6) | **not met** | 33 (3), not met | 36 |
| blind_v2 (40) | hybrid_page 38 | **38** (0) | met | 32 (3), not met | 33 |

Reading: hybrid is level with or ahead of the comparator on two sets and **2 queries short on blind_v1** (needs 38, has 37; one category lost 2). Lexical alone is not enough on
two sets. Against the wiki-only S1 numbers (24 / 36 / 33) lexical search scores 24 / 33 / 32 in the mixed home (0 / 3 / 1 fewer; the wiki also grew by a few pages in between, so this is not a clean attribution).
What this does **not** show: why blind_v1 is short. Two handicaps are known and **not separated** from each other: (1) the query instruction is generic, not the wording tuned
for the wiki (A0 had the tuned one); (2) the home also holds 222 PDFs that compete for the top 5 (A0 was wiki only). The comparator was chosen per set after seeing the sets (a
conservative comparator). With 27 to 40 tasks no significance claim is made. Separating (1) from (2) needs a second recorded attempt (`--again "<reason>"`) on a wiki-only home
or with the tuned instruction; I did not do it because it is a repeat of the baseline sets and the ledger rule says that is Roy's call.

### PDF gap group (development group, n = 30; kb mixed home vs the S-1 baseline system on a PDF-only collection)
| | L1 (right file) | L2 (right file and page) | L2 at rank 1 | L3 (opens at it) |
|---|---|---|---|---|
| kb lexical | 12 | 8 | 7 | 8 |
| kb dense | 27 | 16 | 6 | 16 |
| kb hybrid | 16 | 12 | 8 | 12 |
| S-1 baseline system, hybrid (PDF only) | 28 | 19 | 15 | not measured |
| S-1 dense (PDF only) | 28 | 18 | 14 | not measured |

kb hybrid is **below** the S-1 baseline on the gap group (L1 −12, L2 −7); dense alone comes close on L1 (−1) and L2 (−2) but not at rank 1 (6 against 14).
Diagnosis (gap group only, `docs/s3_diag_gap_mixed.json`, measured): in the mixed home **wiki pages fill the top 5**: 91 of 150 slots under hybrid, 107 under lexical, 63 under dense.
All 14 hybrid L1 failures have a wiki page in the top 5, and the gold PDF is among the top 40 chunks for 29 of 30 tasks. So retrieval finds the paper; the wiki's own pages about
that paper rank above it. Whether that is a defect is a product question: the frozen measure counts only the gold file, but a wiki summary of the paper may be what the person wants.
This is not a claim that the gap is closed, and no weight or chunk change was made on it.

### A cost found by the midterm (measured; not an engineering target met)
The latency recorded in the midterm run (hybrid, resident server, warm): **source view median 738 ms, p95 808 ms (n = 107); chunk view with 40 hits median 4,897 ms, p95 5,276 ms
(n = 30); query embedding alone median 31.5 ms, p95 56.2 ms (n = 40)**. The proposed budget E1 is p95 150 ms: **not met at that scope.**
A profile on made-up queries showed the retrieval itself is about 90 ms (query embedding 52 ms, matrix product 12 ms) and the rest is per-hit work: looking up the position of a
hit scanned all of `conversion_units` (about 113 ms per hit, invisible in tests on small data). Migration 5 adds the missing index (a test reads the query plan so that it cannot
come back unnoticed). **After the fix, on three made-up queries (not the midterm, not tasks):** hybrid source view 182 to 246 ms, chunk view with 40 hits 513 to 785 ms. What remains is
the honest-status check, which re-hashes the original file of every returned hit (about 16 ms each); E1 would need that check cached by file size and time. That changes what the
check guarantees, so I did not do it. **E1 is therefore still not met (190 to 250 ms against 150), and the numbers above the line are the recorded ones.**

## What went wrong, and what it taught (Deviations)
* **The first midterm design was not fit for a one-shot run** (小o round 1: five defects in the script, e.g. re-runnable by environment variable, task file with the acceptance group read into memory, no stop when frozen files failed to verify). All were fixed before a single result existed; the script now records attempts in a ledger.
* **An earlier smoke run** with the old script touched the first 3 tasks of each baseline set and of the gap group; no value was read or recorded (disclosed in `S3_REVIEW.md`).
* **Real data found a bug that 500 tests had not:** a PDF page that repeats one line gives identical chunk windows, hence equal chunk ids, and the commit failed on its own primary key. Equal chunks are now kept once and counted (`repeated_chunks`; 30 of 47,724 here).
* **小o's round 2 found an S1 defect:** `kb rollback` did not check visibility rules (the comment said it would). Fixed.
* **My own netstat bug:** localized headers made the port-owner check silently return nothing under `PYTHONUTF8=1`. Found by running the real thing, fixed, tested.
* **Process:** the full mutant run was started and stopped twice because the code changed after it began; only the last run counts.
* **The midterm script was edited after it ran**, to make three of its checks testable (`check_gap_meta`, `load_gap_tasks`, `check_active_files`; behaviour unchanged). The exact version that ran is kept as `docs/s3_midterm_eval_as_run.py`; its SHA-256 equals the `script_sha256` in the ledger (`b800495d…`).
* **The build report is incomplete:** the first build died at the commit, and the resume logic in the build script lost the intake outcome counts and conversion timing (chunk, text and size counts are intact). It does not affect the evaluation.

## Open points for Roy (each needs your decision, none is mine to make)
1. **Midterm outcome.** The frozen rule is met on 2 of 3 sets and missed on blind_v1 by 2 queries. The plan says the midterm decides whether S5 onward proceeds as planned. Options: (a) go on and carry the miss as a known limit; (b) record a second attempt to separate the two handicaps (tuned instruction / wiki-only home), which repeats the baseline sets by your say-so; (c) work on result mixing (wiki pages vs PDF pages) first, using the gap group only (two rounds left of the frozen budget).
2. **Wiki pages in PDF questions.** ANSWERED by Roy (2026-11-19): yes, a wiki page that names the paper counts. The frozen measure is unchanged; a second, descriptive one is in `docs/S4_REPORT.md` (hybrid 16 → 21 of 30).
3. **E1 (150 ms p95).** ANSWERED by Roy (2026-11-19): cache the check. Done (`BlobStore.verify(cached=True)`, see `docs/S4_REPORT.md`); measured afterwards (2026-11-20, 100 neutral queries, hybrid, quiet machine, `docs/s4_latency.json`): source view median 123.9 ms, p95 177.1 ms; chunk view with 40 hits median 150.4 ms, p95 299.0 ms. **The 150 ms p95 aim was not met by the cache alone.** After running the query embedding and the lexical search side by side (S4 report), the source view p95 is 107.9 ms (aim met) and the 40-hit chunk view p95 287.3 ms (not met; noisy, 140 ms in an earlier run). The midterm figures above were 808 ms and 5,276 ms.
4. Open from S2 (unchanged): H2 gap, upgrade gate thresholds, git init and first commit, level c / purge / merge_tag policy.

## Known limits
* PDF parsing runs in-process, no time or memory limit (小o round 1, item 11); fine for your own PDFs, not for a hostile file.
* Serving checks file sizes only; `kb doctor --deep` hashes everything (a one-byte flip in `vec.npy` is found by `--deep` and not by the quick check, shown by a test).
* The dry-run mode is accountability, not enforcement (see `S3_REVIEW.md`).
* A resident server of an older pin that cannot be proved to be ours is left running and its record is overwritten.
* No CPU fallback; without the server, search is lexical and says so.
* Only Windows 11 and Python 3.13.7 were tested; the `lsof` branch is untested.
* The round-2 fixes (rollback, open pinning, ledger race, gap digest, reuse ownership) have tests and mutants but no third review.
