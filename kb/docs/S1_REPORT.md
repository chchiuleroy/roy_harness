# S1 report (2026-11-16): text class end to end

Evidence labels: **measured** = produced by a run in this session (outputs in `docs/`), **read** = read from code, **inferred**, **guessed**. Everything ran on one machine (Windows 11, Python 3.13.7), once or a few times.

## What S1 delivers
Text, markdown, HTML and CSV go from intake (drop a file or folder, paste text, paste a URL) through conversion, units and statements, chunking, the full commit protocol (staging, verify, journal, atomic rename, one-transaction activation, startup recovery, rollback, gc) to FTS5 search. Wikilinks in prose become deterministic `links_to_source` edges, the first step of the optional graph layer. CLI: `init, add, process, commit, search, status, inventory, rollback, recover, doctor, links`. About 3,900 lines in `src/kb`.

## Completion conditions (Gate 2, stage S1) against results
| Condition | Result |
|---|---|
| text / markdown / HTML / CSV from the three intake routes to search; failures and skips visible (`status`) and an inventory | **Met** (measured). Pilot folder (`roy_km/_raw`, 642 files, reference mode, 0 bytes copied): 342 converted, 1 partial, 299 failed with a reason (298 `unsupported_type`, 1 over the size limit), 0 silent drops. |
| works on `roy_km/wiki` | **Met** (measured): 878 files, all converted, 12,399 chunks, 7,223 wikilink edges resolved, 29 unresolved, 0 ambiguous; ghost check 0 problems; a second build with no change reports `unchanged`. |
| T-IN, T-W2, T-W3, T-K, T-C, T-U, T-WIN pass and each has a mutant that turns it red | **Met** (measured): 291 tests pass (1 skipped: a file symbolic link needs a privilege this account lacks). **198 mutants (82 from S0, 116 for S1), 198 killed, 0 survived, 0 could not apply.** The first full run gave 193 killed, 1 survived, 3 not applicable (their anchor text changed during the review fixes); all four were repaired (a test that matched a folder name instead of the class name; three updated anchors) and killed individually; one more mutant added afterwards was killed individually too. |
| deterministic relations as the first graph step | **Met** (measured on the wiki, see above; hidden sources never appear, tested). |
| relevance not worse than A0 on the baseline sets | **Not met as originally written. Changed to a comparison against A0's BM25 variant, see next section (including its correction: that variant sees only the first 400 characters of each chunk).** |

## Declared change to a completion condition (not silent)
The original condition compared kb with A0's **best** variant (vector or hybrid). S1 has no vector search, so a lexical system cannot be expected to meet it. I changed the check to **against A0's BM25 variant**: kb Success@5 must be at least A0-BM25 minus 1 on each baseline set. The original condition is **not dropped**; it moves to S3 (dense retrieval arrives) and S9 (final evaluation).

**Correction (2026-11-16, found while investigating the blind2 miss): this is lexical against lexical, but NOT like for like in the sense I first wrote.** A0's BM25 scores the stored index, and that index keeps only the **first 400 characters of every chunk** (`wiki_embed.py` line 274, `"text": ctext[:400]`; line 359 feeds the same text to BM25). kb scores the **whole** chunk. A0 also cuts the wiki into 7,706 chunks (mean 110 tokens in its stored text) against kb's 12,399 (mean 134), and uses its own formula (k1 = 1.5, idf = log(1 + x)) while SQLite FTS5 fixes k1 = 1.2 and a different idf. So "A0 minus 1" compares two different lexical systems; it is a reference point, not a controlled comparison.

| Set (valid queries) | kb (lexical) | A0 BM25 (first 400 chars of each chunk) | floor (A0 BM25 minus 1) | A0 best variant (the original bar) |
|---|---|---|---|---|
| real (27) | 24 | 22 | 21 — **met** | 25 |
| blind1 (40) | 36 | 37 | 36 — **met** | 39 |
| blind2 (40) | 33 | 35 | 34 — **missed by one query** | 38 |

* **blind2 missed by one query (measured).** Diagnosis (`docs/s1_diag.json`): of the queries where A0-BM25 hit and kb did not, all have the gold page at rank 6 or 7 in kb (just outside the top 5). I did **not** tune on the baseline sets: doing so would turn the baseline into training data.
* **Follow-up investigation (measured, `docs/s1_diag_prefix.json`, `tools/s1_diag_decompose.py`, `tools/s1_diag_prefix.py`): no single cause; the gap is at noise level.** Both sanity cells reproduce their systems exactly (A0 index with A0 formula: 22 / 37 / 35; kb full text with FTS5: 24 / 36 / 33). Success@5 real / blind1 / blind2 in the other cells: A0 index with the FTS5 formula 21 / 37 / 34; kb full text with the A0 formula 24 / 36 / 33; kb chunks cut to their first 400 characters 24 / 34 / 34 (FTS5 formula) and 24 / 35 / 33 (A0 formula). So (a) the formula matters for A0's index by one query on real and on blind2, and not at all for kb's chunks; (b) giving kb A0's visible text gains one query on blind2 and **loses two on blind1**, so "A0 only sees the start of each chunk" does not explain the gap either; (c) every cell lies within 0 to 2 queries of the others and the sign flips between sets, which at 27 to 40 queries per set is what noise looks like. Corpus drift is excluded: A0's own code run on today's index (878 pages, 7,706 chunks) reproduces its saved lists (top 5 identical for 27/28, 36/40 and 39/40 queries).
* **One query is a steady loser, and its cause was read off the chunks (`tools/s1_diag_a1.py`):** A1 of blind2 ("PLT 距離怎麼算") ranks its gold page 2nd in A0 and 5th to 9th in every kb cell. Both systems pick the same gold section and it matches the same two terms (`plt`, `距離`). A0's chunk starts with the heading `## PLT 框架兩層機制` (A0 splits only at `## `, so the heading and its subsections share one chunk); kb splits at every heading level and its chunk starts at `### Layer 1`, so the parent heading text is **not in the indexed text** (kb keeps the heading path in the locator only). One fewer occurrence of "PLT" in a short chunk is enough to fall behind the paper page `paper_magarshak_2026_kv_cache_plt`. Evidence: read from one query; the general hypothesis (prepending the heading path to each chunk's indexed text helps) is **not tested**, and must be tested on a set that is not the baseline sets, otherwise the baseline becomes training data. Open item for S3.
* Giving kb more text is useful, not harmful: cutting kb's chunks to 400 characters loses two queries on blind1.
* The per-category rule (no category loses more than one query against A0-BM25) holds on the differences listed in the diagnosis: kb won 3 and lost 1 on real, lost 1 on blind1 and 2 on blind2 (different categories each time).
* Against the **original** bar (A0 best variant) kb is below on all three sets (24 vs 25, 36 vs 39, 33 vs 38). That is the expected price of lexical-only and is the S3/S9 target, not a result to hide.
* Scores are identical before and after the review fixes (`docs/s1_wiki_eval_before_review_fixes.json` against `docs/s1_wiki_eval.json`).

## Speed (measured; budget items E are proposals that only become gates at their stated scope)
| Item | Result |
|---|---|
| search over the wiki index, 107 queries, in process | median 106 ms, p95 124 ms, max 174 ms (an earlier run before the review fixes: 84 / 108 / 239 ms; same order, different load). The E1 budget (p95 150 ms) is for a hybrid query over wiki plus PDF, which does not exist yet, so this is **not** a verdict on E1 |
| wiki add / convert / relations / commit / unchanged re-commit | 6.0 / 22.4 / 1.0 / 10.8 / 3.7 s. One run measured intake at 17 s and 35 s on the pilot; the next run gave 6.0 s and 4.6 s, so that was a cold file cache after an hour of other work, not the post-read checks (each costs under 1 ms per file, measured) |

## Review
小o reviewed the S1 code (round 1, static): 8 confirmed findings (3 high) and one remark on the chunker, all real; all fixed, each with a regression test and a mutant (`S1_REVIEW.md`). The fixes were **not** sent back for a second round.

## Known limits and open items
* Folder time-of-check to time-of-use: **narrowed, not closed** (identity, link flags and boundary re-checked after the read). Closing it needs a handle-based open.
* DNS rebinding window in `fetch` (resolve, check, then urllib resolves again): declared, not handled.
* Unit identity is a structural key (kind, heading path, n-th of that kind); inserting a paragraph re-versions later units under the same heading. Content-similarity matching is left to S2.
* Candidate search (uncommitted files) uses simple term overlap, not BM25, and reconverts in memory every time.
* `build` assembles fully to decide "unchanged" (about 3 s on the wiki); the manifest lists every chunk (about 6.5 MB for the wiki) and each process parses it once.
* Windows 11 and Python 3.13.7 only; POSIX paths not run. No compressed archives or media yet, so zip-slip and corrupt-media cases do not apply until S5 to S7.
* Case-only name collisions are tested with the pure function only (NTFS here cannot hold two such names).
* The recovery ghost check and the activation visibility check overlap for a changed profile or tombstone; they are separated in tests by observing the rename.

## Next
Decisions for Roy: (1) accept the blind2 miss as an S3 item; (2) whether to `git init` and commit the `kb` folder (not a repository now, nothing committed); (3) open S2 (proposals, approvals, deletion levels a/b, patches, AI edits) after a Gate 3 look at its row.
