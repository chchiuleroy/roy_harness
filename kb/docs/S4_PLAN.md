# S4 plan (revised 2026-11-19): kb as an overlay layer like superagent, and the optional-install acceptance

**Why this is a rewrite.** The first draft of this file (same day) put a read-only MCP server between the harness and kb. Roy objected twice: kb (FRAG) is not to use MCP, and the way to attach is
**"仿 superagent 的方式"**. Gate 2 section 10 Q2 (how the agent connects) had been recorded as *pending*, with the note that Roy had earlier called the MCP and hook plans unsuitable; I had
recommended MCP there and then treated the question as answered. It was not. This revision follows what Roy said. It is a **proposal to amend Gate 2** (items marked **AMEND**); nothing
below is coded, and the amendments need Roy's OK before any code is written.

> **APPROVED 2026-11-19 by Roy** ("同意，四步全做"): the AMEND items below and all four steps of C10. Implementation starts from this version.

## The pattern to copy (read from `roy_harness/docs/superagent.md` and `src/superagent/`; evidence grade: read, not run by me)
superagent is a separate package that **touches the harness through exactly three lines**: a child process `harness_vllm.py --jsonl <session file>`, a `.harness.hooks.json` it writes
into each ticket's own directory, and an environment it builds itself. `src/harness/` never imports it and it never imports `src/harness/`; deleting `src/superagent/` changes nothing in the
harness (a test runs the harness suite with and without it). Concretely it: frames every piece of outside text as quoted untrusted data (`frame.py`, a fixed header, every line prefixed `> `, so
the text can never start with `/`); runs the harness only in `safe` mode with a whitelist environment; installs a **Mandate hook** in a separate process that vetoes every tool outside the
ticket directory and **vetoes all MCP tools and unknown tools**; takes questions for people into a database and an outbox; writes the model's last text into `outbox/<msg_id>.json` marked
`untrusted: true`; and has three kinds of "remove" (stop, delete the code, purge the data).

## What kb does in that pattern
Retrieval is **deterministic code that runs before the model**, not a tool the model calls: that is what a fast RAG is, and it matches the rule "AI only for judgement". The layer:
1. takes a question (a file in an inbox folder, or a command-line call);
2. runs kb's own search (the same `Searcher`: committed content only, hidden sources never, honest status) and takes the top hits with their source and page;
3. frames the hits and the question as one quoted, untrusted message (superagent's `frame.py` idea, re-implemented in kb because neither package may import the other);
4. starts a harness child process (`--jsonl`, `safe` mode, whitelist environment) in a ticket directory whose hooks veto **every** tool (the model only writes an answer, it calls nothing);
5. writes the answer to an outbox together with the sources it was given (`untrusted: true`; the text is data, not instructions).

The model never connects to kb; kb never connects to the harness's configuration. The harness stays as it is.

## Consequences for Gate 2 (AMEND: each needs Roy's OK)
| Gate 2 item | Now | Proposed |
|---|---|---|
| OPT3 / OPT4 / OPT7 attach and detach through a journal on `.harness.mcp.json`; `attach.py` (S0, tested) | the only contact point is one entry in the harness's MCP config | **Retired.** There is nothing to attach: the three lines above are the contact. "Remove" becomes superagent's three: stop, delete `src/kb`-overlay code, purge data. `attach.py` and its tests stay in the repository, unused, until Roy says delete it (R3, R4, R7 were about it) |
| 4.12 four read-only MCP tools, typed output, must-refuse rules | enforced by an MCP server | **Replaced** by: the layer applies the same rules itself (hidden collections and tombstoned targets never retrieved; ids that do not exist or are out of scope answer the same way) and the model gets **no tools** |
| T-A1 read-only | calls to non-existent write tools refused | the ticket's hooks veto every tool; the layer has no write path to kb; test with a model that tries every tool name |
| T-A2 injection with an obedient fake model and an always-approving fake user | through the real harness and an MCP server | through the real harness and the layer: documents carrying instructions; assert the answer is data in the outbox, no tool ran, nothing outside the ticket directory changed, hidden text never appears |
| T-A3 output size | 30,000-character cut by the harness | the layer caps what it frames and what it records (superagent: 4,000 characters of answer; framed input capped); source fields always complete |
| R1 dependency direction | kb does not know the harness | still true, and now stricter: kb does not import the harness **either**, it only starts it as a subprocess |
| R2 zero cost, R5 lifecycles, R6 capability absence | | unchanged in meaning; R2 compares harness bytes/modules/processes with and without kb installed |

## Completion conditions (written before the work, judged at the end)
| # | Condition | How it is judged |
|---|---|---|
| C1 | **Contact is exactly the three lines**; no file of the harness is written, no harness config is edited; the harness repository is untouched | a test that snapshots the harness tree and the harness's own config before and after a run |
| C2 | **Retrieval before the model, with kb's rules**: hidden collections, tombstoned targets and out-of-scope ids never reach the prompt; a missing id and a hidden id are answered identically | tests with canary text in hidden sources |
| C3 | **Everything outside is framed as data**: instruction-like text in documents and in the question cannot start a line with `/` and is quoted; the framing has a fixed header and footer | tests copied in spirit from superagent's `frame` tests |
| C4 | **The model has no tools**: the ticket's hook vetoes every tool call including unknown and MCP ones; the harness runs in `safe` mode with a whitelist environment | T-A2: scripted obedient model plus an always-approving user, through the real harness |
| C5 | **Answer and sources are recorded**: each outbox message holds the answer text (marked untrusted, capped), the list of sources and pages it was given, and the commit id; the same message id is never written twice with different content | tests on the outbox contract |
| C6 | **Optional by construction**: with the layer's code deleted the harness suite gives identical results; with kb installed but the layer not running, the harness shows no difference (R2); the three removals work (stop leaves no child process; delete code changes nothing in the harness; purge removes the data) | the harness removal comparison and R5 tests |
| C7 | **Real harness end to end**, once, by a person: a question is answered from kb with the sources shown; stop; the harness tree is byte-identical to before | a recorded run in `docs/S4_E2E.md` (hashes before and after, the question, the answer, the sources) |
| C8 | Every test has a mutant that turns it red; all earlier mutants still killed | `tools/mutate.py`, full set |
| C9 | Review by 小o (contract v1.1) of the layer and of the end-to-end procedure before the end-to-end is run | `docs/S4_REVIEW.md` |
| C10 | **A misleading answer is made hard to produce and easy to catch** (proposal, see "Defending against a misleading answer" below): quotes checked verbatim by code; passages labelled by origin and screened for instruction-like text; the passages shown beside the answer by the layer, not by the model | tests with an obedient fake model and poisoned passages; the report counts how many poisoned cases were flagged, dropped or passed |

Things this stage does NOT do: any MCP server or client for kb; any tool for the model; any write to kb from the model (writing stays with the person and the proposal flow); change the
harness; images, audio, video; the graphical front end. **Accepted remaining risk (unchanged in spirit):** a model that has read hostile text can still write a misleading *answer*;
the layer shows the sources next to it so a person can check, and says in the report that this is not prevented.

## Defending against a misleading answer (proposal for C10; what each step does and does not give)
A model that has read hostile text cannot be stopped from *writing* something misleading, but the layer can make that detectable. Four steps, all deterministic code, in order of value:
1. **Quotes must be verbatim, and the code checks them.** The prompt asks for every claim to carry a quote and the chunk id it came from; the layer checks that each quote is a literal substring of that chunk. A claim whose quote is not found is marked unverified in the outbox (or the answer is withheld, a setting). This stops *invented* support. It does **not** stop a true quote used misleadingly.
2. **Label every passage by where it came from, and screen it.** kb already records origin and approval state; the framed prompt tells the model which passages come from the person's own approved material and which from material pasted or fetched from outside, and the outbox lists them separately. A simple screen (instruction-like patterns such as "ignore the above", tool names, commands) flags a passage; a flagged passage is shown to the person and, by default, left out of the prompt. This is a heuristic: it catches clumsy attacks, not clever ones.
3. **The layer, not the model, shows what the model was given.** The outbox always carries the retrieved passages with file, page and a `kb open` target, whatever the model wrote about them. The person can check the answer against the very text it was built from, in one step.
4. **A person confirms before an answer from flagged or outside-origin passages is used** (outbox kind that waits, as superagent's questions do). Optional second step, not in this stage: a second model call that only checks "does each sentence follow from the quoted passages" (a judge with the usual self-preference bias, so it adds a check, not a guarantee).
What remains after these: a clever poisoned passage from a source you approved yourself, used with a true quote. That is the accepted remaining risk; steps 3 and 4 are what keep it visible.

## Open points, with defaults (none blocks starting once the amendments are approved)
* **Where the layer lives.** Default: a new package `kb_overlay` (name proposed) inside the kb repository, tier above the core, imported by nothing in the core. Roy may prefer a separate directory.
* **Which model endpoint the end-to-end uses.** Default: the vLLM endpoint configured in `roy_harness/.harness.env` (not read or printed by me), `safe` mode, one question.
* **Whether the answer is also offered to the person's own tools** (a file in an outbox folder is all this stage does).
* Deleting `attach.py` and its tests: not now.
