# S3 review by 小o (rounds 1 and 2): findings and handling

Reviewer: 小o (Codex, gpt-5.6-sol, read-only, static: no shell, no tests run), output contract v1.1. Each finding below was checked against the code by 小c before
it was accepted; "fixed" means a test (and a mutant) now guards it. Prompts and raw answers are kept outside the repository (scratchpad) and summarised here.

## Round 1 (before any midterm number existed): the method and the S3 code

小o's verdict: **do not run the midterm yet.** All of the following were confirmed by 小c against the code.

| # | Finding (小o) | Handling |
|---|---|---|
| 1 | The "once only" rule of the midterm script could be bypassed (environment variable, overwrite of the result, a failed run left no trace); the `SMOKE` mode ran real tasks | Rewritten. An attempt is appended to `docs/s3_midterm_attempts.jsonl` before any task is read; a second one needs `--again "<reason>"`; results are created exclusively (`s3_midterm_run<N>.json`); dry runs use made-up tasks only. **Disclosure:** before the rewrite, the old smoke mode had been run once on the first 3 tasks of each baseline set and of the gap group; no hit value was read, printed or recorded. |
| 2 | A failed check of the frozen files did not stop the run | Now an `Abort` before the home is opened; recorded as `aborted`. |
| 3 | The baseline cohort was filtered by "gold page exists now" instead of the frozen definition of a valid task | Cohort = A0's own `gold_missing_from_index == False`; the script aborts unless it equals the page-existence filter and the comparator scores equal the frozen 25/39/38 (checked offline first: 27/40/40 tasks, same). |
| 4 | Gold matched by file-name stem could be fooled by a name collision | Abort unless all source name stems in the home are distinct under NFC + case-folding (real home: 1,104 sources, 1,104 stems). Task ids must be unique (28/40/40). |
| 5 | The acceptance group was read into memory by the evaluator (filter after load) | The gap group is its own file, cut once by `tools/s3_make_gap_tasks.py` (30 of 48 tasks); the evaluator never opens the frozen task file, it only hashes it (a test guards that). |
| 6 | After `kb embedder set` an old resident server was reused and its vectors labelled with the new model | `server.json` carries a fingerprint (server, model, model hash, flags); a server that does not match is not used. |
| 7 | `stop` only proved that something answered on the port, not that it was the recorded process | The port's listener must be the recorded process id (`netstat -ano` on Windows, `lsof` elsewhere); if it cannot be told, nothing is ended. **Found by a real run:** netstat's headers are localized (cp950) and text-mode decoding under `PYTHONUTF8=1` returned no output at all; it now reads bytes. |
| 8 | Clean-up after the activation (vector cache, retired history, opened copies) was not recoverable if the process died in between | Every `kb commit`, also when nothing changed, runs the clean-up again (`_after_publish`). |
| 9 | `kb open <chunk id>` accepted old or hidden chunk ids | The chunk must belong to the active commit and its source must not be hidden at level b (checked inside the opener, so every caller gets it). |
| 10 | Serving checks sizes only; a same-size corruption of `vec.npy` is not seen | **Not changed, on purpose:** hashing 234 MB at every query is not affordable. `kb doctor --deep` hashes every artifact of the active commit; a test shows that a one-byte flip is found by `--deep` and not by the quick check. The evaluator hashes the active files before scoring. |
| 11 | PDF parsing runs in the main process, no time or memory limit | **Not changed.** Known limit: the PDFs are the owner's own; isolation needs a worker process (a new design). |
| 12 | Tests and mutants did not guard the above | Tests and mutants added for each fixed item. |

## Round 2 (before the midterm run): the fixes of round 1

小o confirmed round 1's items 2, 3, 4 (stem exactness) and the byte-level netstat fix as closed, and found the following.

| # | Finding (小o) | Handling |
|---|---|---|
| 1 | **`kb rollback` did not compare the visibility rules of the commit it returns to** (an S1 defect: the comment in `prune_stale_history` said rollback "would be refused anyway", it was not). After a cut-off activation the retired commit with hidden content could be restored | Fixed: rollback refuses a commit assembled under visibility rules that no longer hold. Test + mutant. |
| 2 | No cross-process guard on the attempt ledger | Fixed without a lock: each attempt line carries a token; after appending, the first line with that number proceeds, any other stops before reading a task. |
| 3 | The dry run (`--synthetic`) could be fed real tasks | Fixed in part: it aborts if any of its queries equals a real task query. **Remaining:** it still queries the real home and may start the embedding server, and a paraphrase of a real query would pass; this is accountability, not enforcement. |
| 4 | The gap file only claimed the digest of its source | `tasks_sha256` and the count (30) are checked. **Remaining:** the digest sits in the same file as the tasks; the ledger also records the file's own hash at run time. |
| 5 | `kb open` on a chunk opened the newest revision of the source, not the one the chunk was cut from | Fixed: the chunk's revision is pinned (a stored copy opens the old bytes; a changed original is reported `source_changed`). |
| 6 | Reusing a resident server did not require that the recorded process owns the port | Fixed (`_listener_pid` in the reuse test too). |
| 7 | The gap file was read before the attempt was written | Fixed: the attempt line is the first thing written; provenance (script, gap file, frozen file hashes) follows. |
| 8 | Hidden-source check lived in the CLI, not in the opener | Moved into the opener. Level a is not blocked before the next commit, the same as the open-by-name path. |
| 9 | Result not bound to the ledger | The completed record carries the result file's SHA-256; the file is fsynced. |
| 10 | The evaluator did not hash the active artifacts before a once-only run | It does (`verify_artifacts`) and aborts on any problem. |
| 11 | A server that cannot be proved ours is left running, and its state is overwritten | **Not changed (low):** safe against killing a stranger, but a stray server of an old pin can stay alive; `kb embedder status` shows only the current one. |
| 12 | Several mutant definitions pointed at text that no longer existed | Repaired (8 anchors); a check script confirms all 401 anchors match exactly once. |
| needs_validation | Does the CLI block both tombstone levels for a chunk? Are task ids unique? Do stems collide under Windows comparison? | CLI blocks level b only, by design (same as by name); ids unique (28/40/40); stems unique after NFC + casefold (1,104/1,104). Guards added. |

Rejected by 小o itself: `_after_publish` on an unchanged build is not harmful by itself; a legacy server file without fingerprint is not reused silently; the byte parser is
sound for its fixed format; warm-up and the L3 URL check do not touch the tasks.

**Not sent back:** a third round on the round-2 fixes (same as S2). The fixes of items 2, 3, 5, 6, 8, 9, 10 have tests and mutants but no independent review.

## What this review changed in the plan
The midterm ran once, after both rounds, with a script that no longer allows the things round 1 found. Round 2 found one defect in S1 code (rollback) that three earlier
reviews and two hundred tests had not.
