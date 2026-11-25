# S2 report (2026-11-16 to 17): proposals, approvals, patches, deletion, AI corrections

Evidence labels: **measured** = produced by a run in this session (outputs in `docs/`), **read** = read from code, **inferred**, **guessed**. One machine (Windows 11, Python 3.13.7), once or a few times.

## What S2 delivers
A change to the knowledge is a **proposal** the user approves; nothing else writes. On top of S1: `review` (create, approve, apply, reject, stale detection, retention of old differences), **deletion levels a and b** (a: not searchable; b: also hidden from general access; the original file stays), **patches** (human corrections that follow their paragraph when the converter's output changes and go to a person when unsure), an **AI correction flow** (scripted proposer; no model is connected), **incremental work** (one changed source costs one conversion and one chunking) and **converter upgrades** (tried on a sample first). CLI: `show`, `delete`, `patch`, `review list|show|approve|reject|resolve|purge`.

The review rules are **enforced by database triggers** (migration 4), not only by Python: no tombstone without an approved delete proposal that names that target and level; a proposal moves along legal states only and cannot be edited after it was made; a human or AI statement becomes part of a commit only with an approved proposal of a fitting kind that the statement itself names. They stop a bug or a careless future code path in kb; they do not stop someone who can write the database file directly.

## Completion conditions (Gate 2, stage S2) against results
| Condition | Result |
|---|---|
| T-D1 deletion needs approval | **Met** (measured). Database-level tests (unapproved, wrong kind, state-without-approval-row, another target, higher level, level c, edited proposal) and a code scan showing `review.py` is the only writer of tombstones |
| T-D2 a tombstone takes effect | **Met** (measured). Not before the next commit; afterwards gone from search (source and chunk views), the relation graph and `kb show` (level b), original blob verified and present; unit-level hiding; older versions that still hold the text are removed |
| T-D3 where the removed text may and may not remain | **Met for the classes that exist, with an open point (below)** (measured). A unique string is scanned across raw blobs, index segments, staging, backups, attach journals, logs, and the proposal and audit tables; expectations per class are asserted for level a and level b, and again after the retention period. A negative control shows the scan finds the string before hiding |
| T-P1..P4 patches | **Met** (measured). Anchor still accurate: followed automatically (also several corrections in order); paragraph changed a lot: queue, nothing applied; paragraph gone: orphaned; unrelated boilerplate or identical copies: a person decides; proposal made against an older commit: stale |
| T-AI1 AI proposals | **Met** (measured). Unapproved proposals leave every table unchanged; after approval only the knowledge layer changes (original and converter output hashes equal); who asked and who approved are recorded; invented citations cannot be approved; human-written content is protected unless the approval carries an explicit override; hidden content is not shown to a proposer; the approval screen shows old and new value and the evidence |
| T-INC1 incremental work and upgrades | **Met** (measured). One edited source of eight: 1 conversion, 1 chunked, 7 reused; a correction re-chunks only its source; hiding re-chunks nothing; an upgrade that changes nothing converts every source again and recomputes no chunk; one that changes everything, or only the order of units, is refused with nothing converted |
| every test has a mutant that turns it red | **Met: 289 mutants (S0 82, S1 116, S2 91), 289 killed**, the whole set re-run after the round-2 fixes. See "Mutation run" below |

**Tests: 416 pass, 1 skipped** (a file symbolic link needs a privilege this account lacks).

## Mutation run (measured) and what went wrong in it
The first full run died on a timeout (a mutant's test run exceeded the limit and my script did not handle it). The second run finished with 278 killed, 3 survived, but 4 of the "killed" were timeouts: four mutants running in parallel timed out at the same moment, which looks like the laptop being suspended or stalled, not four hanging suites. A timeout proves nothing, so I made `tools/mutate.py` report timeouts as inconclusive and re-run them alone with twice the time.
* 3 mutants survived: tests for the tombstone kind check and the approval-row check were too weak (the proposal in them was refused for another reason first), and the assembly-level binding of a statement to its proposal was not tested on its own. All three were strengthened and killed.
* The 4 timed-out mutants and the 3 survivors were re-run (7 in all): 7 killed. The other 274 results are from the full run.
* After the round-2 fixes the whole set (289 mutants, S0 to S2) was run again: 287 killed, 1 survived, 1 could not be applied. The one that could not be applied (`s2_hidden_level_b_is_shown`) had an anchor I edited after the run began; the survivor (`s2_hidden_level_ignores_a_unit_id`) showed that nothing tested a delete proposal whose target is a unit id once that unit is hidden at level b. I added that test (`test_a_pending_delete_of_a_unit_does_not_show_the_unit_once_it_is_hidden_at_level_b`); both mutants re-run alone: killed. Final: 289 of 289.
* Machine note: the suite takes 60 to 80 seconds alone and much longer when the machine is busy or asleep; do not let the laptop sleep during a mutation run.

## Review
小o reviewed the S2 code (round 1, static): **8 confirmed (2 critical, 5 high, 1 medium) and 1 needs_validation**. All eight were real and are fixed, each with tests and mutants (`S2_REVIEW.md`). The two critical ones were mine: my triggers checked that SOME approval existed but did not tie it to what was approved (one delete approval could hide anything; a delete approval could vouch for an AI statement). A second round (also static) held up the main fixes and found **3 new high findings, all real and fixed** (see `S2_REVIEW.md`, round 2): a patch could still be moved automatically when only the heading matched; `kb links` and the approval screen of a proposal made before hiding still showed hidden level-b content; the link from a committed statement to its proposal could be rewritten. The round-2 fixes have **not** had a third review.

## Found by my own tests, not by the review
1. When I let a newer converter version reconvert the same revision, `_previous_unit_ids` lost the previous conversion (it was the one being written): every unit got a new identity. The suite caught it at once.
2. `purge_expired` read a UTC time as local time (wrong by the machine's offset from UTC; 8 hours in Taiwan). Dates months apart could not show it; an exact-boundary test does.
3. The approval screen for a deletion showed only an identifier, so the user could not see what they were deleting. It now shows the file name and title.

## Real-data check (measured)
Re-run of `tools/s1_eval.py` over the wiki: Success@5 real 24, blind1 36, blind2 33, **identical to before S2**; ghost check 0 problems; a second build with no change reports `unchanged`. The wiki grew in between (12,399 to 12,436 chunks). Timings (commit 10.8 s to 24.5 s, search median 106 ms to 116 ms) were taken while another process was using the machine, so I draw no conclusion from them and do not claim a speed change either way; they need a re-measure on a quiet machine.

## Open points for Roy
1. **Gate 1 H2 gap.** After hiding at level a or b the derived database rows (`chunks.text`, `statements.value_json`) still hold the hidden text, and a pre-migration backup is a copy of the whole database. H2 says the string should be found "nowhere in the application folder". The scan asserts these two classes as kept on purpose (like the original blob). Either that is accepted, or hiding must also clear derived text, which would make a hide harder to undo.
2. **Upgrade gate thresholds** (mean distance 0.05, single source 0.5, sample 10) are my proposals, not measured.
3. **Level c (physical erasure), `purge` and `merge_tag`** are refused or absent until the policy is decided.
4. **The support judge** is a baseline: on 20 samples labelled by me alone (no independent annotator) it scores 55 percent, 0 of 5 on paraphrases, accepts 1 claim that changes a single word of the evidence and flags 8 faithful ones. That is why support never blocks an approval and existence of the citation is the hard rule.
5. A third review round on the round-2 fixes (they are smaller than round 1's but touch the approval screen, the trigger set and patch replay).

## Known limits
* "Which edits are factual" is decided by key name (`text`, `summary`, `note` must cite). A rule of thumb, not a classifier.
* The proposer is a scripted function. When a real model is connected, an integration test must show the caller cannot choose `external=False` for an external provider (小o's needs_validation item).
* The inventory scan covers the classes that exist now. Caches, thumbnails and ASR or OCR temporary files arrive with stages S5 to S7 and must be added to it then.
* Patches work on one unit's text; a correction that spans units or changes structure is out of scope.
* The S1 limits stand (folder time-of-check gap narrowed, not closed; DNS rebinding window; Windows 11 only).
