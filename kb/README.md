# kb

An **optional** knowledge layer: the write side (convert, metadata, chunk, approve, delete) and the read side (search) for text, PDF, images, audio and video.
Status: **S4 (question and answer through a harness process, the way superagent sits on the harness)** on top of S3 (PDF, dense retrieval, hybrid ranking, `kb open`, midterm judgement), S2 (proposals, approvals, patches, deletion, AI corrections) and S1 (text class end to end): text, markdown, HTML, CSV and PDF go from intake to search (lexical, dense or hybrid); every change to the knowledge is a proposal the user approves. Images, audio, video and a graphical front end are later stages. Design: `C:/Users/roy/plan_gate_knowledge_system_gate1.md` (what and why), `..._gate2.md` (modules, interfaces, tests, stages).

## What exists now
| Piece | Module | Why it exists |
|---|---|---|
| Identifiers | `ids.py`, `canonical.py` | Deterministic ids (revision, unit version, statement, chunk) from a canonical preimage; random ids for sources, units, proposals |
| Database | `schema.py`, `db.py`, `writer.py`, `home.py` | SQLite (WAL); all tables of Gate 2 section 4.2; append-only and frozen-membership rules enforced by triggers; atomic migrations with verified backup; single-writer OS lock |
| Text key | `keys.py`, `ids.py` | Every hash of text (fingerprint, unit version, statement and chunk ids, conversion and memo hashes) is an HMAC under a 32-byte key in `text.key` in the data folder, not in `backups/`: after a hide a copy of the database without the key cannot be used to confirm a guessed paragraph. A missing or foreign key is refused at connect; the whole data folder is copied as a backup, so the key goes with it (see `docs/S4_KEYED_IDS.md`) |
| Raw evidence | `cas.py` | Content-addressed store (copy or reference mode); no delete API |
| Audit | `audit.py` | Event types and identifiers only, never original text |
| Privacy profile | `profile.py`, `colls.py` | Per-collection policy; `secret` cannot be loosened; strict loading; enforcement functions |
| Health | `doctor.py` | Which optional capabilities exist and what a missing one costs; data-folder integrity |
| Harness connection | `ask_runner.py` | The only point where kb touches the harness: a child process (`--jsonl`), a hook file in the ticket folder and an environment kb builds itself. The old `attach.py` (MCP entry and journal) was retired in S4 |
| Intake | `inbox.py`, `pathsafe.py`, `fetch.py` | Drop a file or folder, paste text, paste a URL; stable-file window, locked files deferred, nothing dropped silently (`intake_skips`); Windows path hazards refused with a reason; URL fetch limited to public http/https with a size cap |
| Conversion | `conv_*.py`, `pipeline.py` | text, markdown, HTML, CSV to units and statements; formulas and scripts are kept as text, never run; unit identity survives edits |
| Chunk and commit | `chunker.py`, `display.py`, `commit.py` | Staging, verify (ghost check against the database), journal, atomic rename, one-transaction activation, startup recovery, rollback, gc |
| Search | `tokenize.py`, `ftsindex.py`, `search.py` | SQLite FTS5 over pre-tokenized text; modes lexical, dense, hybrid (RRF) and auto; every hit says why it matched and whether it is current, outdated or only a candidate |
| PDF | `conv_pdf.py` (tier T2, needs PyMuPDF) | Text blocks with page and box; chunks never span pages; encrypted, damaged or text-less files are visible rows |
| Vectors | `embedder.py`, `vecindex.py`, `dense.py` (tier T1, needs numpy and a local llama-server) | A resident server on 127.0.0.1 with the model pinned by hash; vectors are a second artifact of the same commit and a different model, dimension, pooling or instruction is refused |
| Ask | `ask_*.py` (tier T3) | `kb ask`: retrieval by kb's own rules first, everything from outside quoted as data, one harness child process whose hook vetoes every tool, quotes checked against the passages, the passages the model was given listed by kb, an answer held until a person releases it when anything weakens it. kb imports nothing of the harness and the harness knows nothing of kb; no MCP |
| Open | `opener.py` | `kb open` hands the viewer the original file at the page of a hit, pinned to the revision that was indexed; a moved or changed original is reported, not opened |
| Relations | `relations.py` | Wikilinks in prose become deterministic `links_to_source` edges (first graph step); hidden sources never appear |
| Review | `review.py`, `patches.py`, `evidence.py`, `ai_edit.py` | Proposals, approvals, deletion levels a and b (the original stays; the text derived from what is hidden is erased at the next commit from the database, from the files kb derives from it and from the pre-migration backups kb makes itself; copies of the data folder that you made yourself are not touched and still hold it), human corrections that follow their paragraph, AI edits that need real citations; the rules are enforced by database triggers (schema migration 4) |
| Incremental | `upgrade.py`, chunk memo in `commit.py` | One changed source costs one conversion and one chunking; a new converter version is tried on a sample first |
| CLI | `cli.py`, `inventory.py` | `kb --home DIR init, add (--agent-visible, --allow-external-model for a new collection), process, embedder (set, status, stop), embed, commit, search, open, ask, answers (list, show, release), collection (list, set), status, inventory, show, delete, patch, review (list, show, approve, reject, resolve, purge), rollback, recover, doctor, links` (no default data folder, on purpose) |

## Optional by construction
* The harness does not know kb. `tests/test_layers.py` scans the harness source with a syntax tree (imports, dynamic imports, subprocess commands) and requires zero references.
* Tiers (`layers.py`): T0 core is standard library only; higher tiers may import lower ones, never the reverse; every module must declare its tier.
* Not attached, or not installed, costs the harness nothing observable (`tests/test_zero_cost.py` compares module set, subprocesses, sockets, opened files, prompt and tool-schema hashes).
* `kb attach` (the old MCP attach journal of S0) is no longer part of the design: kb attaches like superagent, through a child process, a hook file in the ticket folder and an environment it builds (S4). The module and its tests stay until Roy says delete.

## Tests
```
python run_tests.py                 # whole suite (standard library unittest)
python tools/mutate.py -j 3           # break the code on purpose; every mutant must be caught by a test (hours)
python tools/measure_o2.py          # timing noise of the harness import (quiet machine)
python tools/removal_compare.py     # harness full suite with and without kb (about 25 minutes)
```

## Not done yet (by stage)
S4 real end-to-end run on the harness (written down in `docs/S4_E2E.md`, needs an endpoint) - S5 images - S6 audio - S7 video - S8 other entry points - S9 front end and final evaluation.
`s1/` holds the S-1 baselines, frozen task sets and thresholds (`python s1/freeze.py --verify`).

Reports: `docs/S0_REPORT.md`, `docs/S0_REVIEW.md`, `docs/S1_REPORT.md`, `docs/S1_REVIEW.md`, `docs/S2_REPORT.md`, `docs/S2_REVIEW.md`, `docs/S3_PLAN.md`, `docs/S3_REPORT.md`, `docs/S3_REVIEW.md`, `docs/S4_PLAN.md`, `docs/S4_REPORT.md`, `docs/S4_REVIEW.md`, `docs/S4_E2E.md`.
The midterm (`tools/s3_midterm_eval.py`) may be run once; attempts are on the record in `docs/s3_midterm_attempts.jsonl`.
