# S3 plan: PDF, dense retrieval, hybrid ranking, `kb open`, midterm judgement (opened 2026-11-17)

Source of the stage: `plan_gate_knowledge_system_gate2.md` section 8 (row S3) and section 7 (T-I2, T-S1, T-H6). Rules for the midterm: `s1/judging_rules.md`
and `s1/thresholds.json` (frozen in S-1; the acceptance group stays untouched until S9).

## Completion conditions (written before the work, judged at the end)
| # | Condition | How it is judged |
|---|---|---|
| C1 | A PDF is converted with the page and box of every block; chunks never span pages; failures (encrypted, damaged, no text layer, missing PyMuPDF) are visible rows, not gaps | tests `test_pdf.py`; conversion report of the real PDF corpus (counts of failed, partial, pages without text) |
| C2 | T-I2: vectors built under another model, dimension, pooling or instruction are refused, never mixed in; the way back (`kb embed`, `kb commit`) works | `test_vectors.py`, `test_cli_s3.py` |
| C3 | T-S1: lexical and vector index hold exactly the commit's members; hiding removes a chunk from both and from every result mode; a vector row can never disagree with its member | ghost check on the vector artifact, `test_vectors.py` |
| C4 | T-H6 (PDF): a hit opens to the right file and page; a moved or changed original is reported and not opened | `test_cli_s3.py`; on the gap group, L3 = the target handed to the viewer equals the gold (file, page), computed by program |
| C5 | Hiding leaves no named copy of the original behind (`opened/`), and no vector of hidden text in the cache after the next commit | inventory classes and tests |
| C6 | Embedding backend: the model is pinned by hash, the server listens on 127.0.0.1 only, one slot, resident memory reported; no text goes anywhere else | tests; measured resident memory and query latency on the real corpus |
| C7 | Every test has a mutant that turns it red (all earlier mutants still killed) | `tools/mutate.py`, full set |
| C8 | Midterm: A (kb, hybrid) against A0 on the BASELINE sets once, with the frozen rule, reported descriptively; PDF gap group reported L1/L2/L3 against the S-1 baseline system | `tools/s3_midterm.py`; result decides whether S5 onward goes as planned |
| C9 | Review by 小o (contract v1.1) of the S3 code and of the midterm method before the midterm numbers are written down | review file in `docs/` |

Things this stage does NOT do: tune anything on the baseline sets or the acceptance group; claim the page-location gap is closed; OCR, audio, video.

## Step 1 result: diagnosis of the S-1 page-location gap (gap group only, n=30; `tools/s3_diag_gap.py`, `docs/s3_diag_gap.json`)
S-1 left this open: dense search found the right file for 28 of 30 tasks but the right page for 18. The diagnosis classified the 12 failures
(evidence grade: measured on the S-1 vectors with the resident server; 30 tasks, so descriptive only):

* **The gold page is not a duplicate of another page.** In none of the 12 failures does a page ranked above the gold share half of the gold page's text
  (8-gram containment; the largest value is 0.178). So "the gold is ambiguous because copies exist" is **ruled out** as the main cause.
* **Extraction is not empty.** Every gold page has at least 1,173 characters of text. Whether the text is garbled (maths, tables) was not tested.
* **The failures sit in the vague query types.** Per category (L2 at 5): exact_term 5/5, mixed_zh_en 7/9, paraphrase 5/10, partial_memory 1/6.
* **Two kinds of failure.** Four near misses (gold ranked 6th to 9th, a neighbouring or same-file page above it) and eight far misses (rank 12 to 119, other
  files score higher than the gold page, 0.50 to 0.76 against 0.42 to 0.61 for the gold). In 6 of the 12 failures a page directly next to the gold page is in
  the top 5 (so with a tolerance of one page the score would be 24/30; **descriptive only**: the frozen tolerance is 0 pages and stays 0).
* **Not isolated:** the quality of the queries (written by 小o from a page excerpt; some are very vague, e.g. "that part where the AI tests internal service
  permissions after finding a credential") and the chunk granularity. The diagnosis does **not** show a defect in chunking or page aggregation, so no chunking
  change is justified by it; what it shows is that dense similarity of a short Chinese description to an English paper is a weak signal for the page.

Consequence for S3: PDF page-level results are reported as measured, the gap is not claimed to be closed, and any change to chunking or weights on the PDF set
uses the gap group only, at most two rounds (frozen budget).

## Design decisions made by 小c (reasons; Roy may veto)
1. **Vectors are a second artifact of the same commit** (`vec.npy`, rows in the order of the sorted member chunk ids, float32, one unit-length row per member) with
   the model identity in the manifest. The lexical index and the vectors can then never describe different worlds (F22, T-S1), and rollback restores both.
2. **Embedding happens ahead of the commit** (`kb embed`, resumable, cached by text hash) because it takes hours on a big collection; the commit embeds at most
   200 chunks itself and otherwise stops with the number to embed first. Nothing is published half way.
3. **The embedding server is resident, 127.0.0.1 only, one slot, 4096 tokens** (S-1 measurement: about 0.8 GB, 41 to 53 ms median per query). The model is pinned by
   hash (`kb embedder set`), which is the approval step; a replaced file refuses to run. No CPU fallback is built (S-1 measured it at 251 ms median, 1.8 s p95); without
   the server, search falls back to lexical and says so.
4. **The query instruction is generic** ("Given a search query, retrieve relevant passages that answer the query"), not the wording tuned for Roy's wiki, because kb is
   a general layer. This is a handicap against A0 (its wording was written for the wiki); it is part of the identity, so changing it makes a new index.
5. **Optional tiers stay optional:** `conv_pdf` is tier T2 and registered by the command line at start; `embedder`, `vecindex`, `dense` are tier T1 and imported only
   when used, so kb without numpy or PyMuPDF still runs everything else.
6. **`kb open` hands a `file:///...#page=N` URL to the default viewer** (`--launch`). kb can check the target, not where a viewer finally lands; Chrome, Edge and Firefox
   honour the fragment, another viewer may not.
