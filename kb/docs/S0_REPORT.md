# S0 report (2026-11-16): skeleton

Evidence labels: **measured** = produced by a run in this session (outputs in `docs/`), **read** = read from code, **inferred**, **guessed**. Everything was run on one machine (Windows 11, Python 3.13.7), once.

## Completion conditions (Gate 2, stage S0) against results
| Condition | Result |
|---|---|
| Layer-P tests for the skeleton pass: ID contract (T-ID1), migrations (T-M1..M4), single writer (T-W1), profile enforcement points, immutable evidence and audit | **Met for what exists.** 119 tests pass. Partly open: T-F1 covers the enforcement functions only (their callers arrive in S1+), T-W1 covers the lock and non-blocking readers (the commit protocol is S1). |
| R1 dependency direction (static analysis) | **Met.** Tier table + syntax-tree scan of kb; real harness source scanned: standard library and its own folder only, no reference to kb, no exec/eval, no non-literal dynamic import. Scanner is a guard for known syntactic forms (stated in its docstring), not a proof. |
| R3 / R4 / R7 attach, detach, crash recovery | **Met.** Exact byte round trip (CRLF, tabs, absent file), same-name refusal, refusal after later edits with a diff, `--entry-only` only for an unchanged entry, `--take-over` explicit, crash kill at every point of attach, exact detach and entry-only detach; the real harness loader (`harness_mcp.load`) is the oracle for "still loadable". |
| R2 zero cost | **Met for the observations listed.** Not installed vs installed-not-attached: identical module set, subprocesses, sockets (bind, listen, connect), opened files, started threads, changed environment, signal handlers, system-prompt hash, tool-schema hash, MCP config. Nine negative controls show the probe sees each kind of difference. It does not measure CPU or memory. |
| Every layer-P and optional-install test has a mutant that turns it red | **Met: 82 mutants, 82 killed, 0 survived, 0 could not apply** (`python tools/mutate.py -j 4`; run after the review fixes). |
| Measure and freeze the O2 tolerance and the ghost-check sampling ratio | **Done.** Ghost check: verify all chunks at every commit (0.21 s for 31,980 chunks). O2: N = 1, tolerance 51.9 ms on a 290 ms median, so the timing criterion only detects slowdowns of about 50 ms or more (the noise is large); the exact observations above are the precise half. See `S0_MEASUREMENTS.md`. |

## Behavioural evidence for "the harness does not know kb" (measured)
The harness's own full suite, run once without kb importable and once with kb on `PYTHONPATH`: **1173 tests, identical per-test outcomes (1146 ok, 27 skipped, 0 differences)**; the harness repo stayed clean (`docs/s0_removal_compare.json`). Limit: the harness never imports kb, so this mostly confirms the static result; it does not exercise an attached kb.

## Review
小o reviewed the S0 code: 10 confirmed findings, all real, all fixed (`S0_REVIEW.md`), including a high-severity hole that let a published commit's members be moved out by an UPDATE. The fixes were **not** sent back for a second review round.

## Bugs found by the tests themselves
1. `allowed_converters: "pdf"` in a stored profile became `('p','d','f')` (three single-letter converter names), silently replacing the policy. Fixed at load.
2. A connection leak in `doctor` (found by tracemalloc).
3. One redundant rule in the tier table (an explicit sibling clause that the level comparison already implied) removed after a mutant showed it was dead code.

## Known limits and open items
* Power-loss durability cannot be tested here; directory sync is POSIX-real and Windows best-effort (`durable.py`).
* No lock on the harness config between reading and replacing it in `attach` (documented; the window is milliseconds and the action is explicit).
* POSIX paths (flock, directory fsync) were not run. The stdio MCP server does not exist yet, so a real attached session is S4.
* Journal integrity protects against corruption and edits, not against a local attacker who writes both the journal and matching digests.
* The O2 timing noise is high (about 18 percent of the median).

## Next: S1 (text-class end to end)
text / md / html / csv; entry points drop file or folder, paste text, paste URL; conversion, units, statements, chunking, the full commit protocol (Gate 2 section 4.10), FTS5 search, CLI; deterministic relation statements (wikilinks, source links) as the first graph step. Completion: the S1 row of Gate 2 section 8.
