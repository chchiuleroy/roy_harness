# Keyed hashes of text (S4, eighth review, decision of Roy 2026-11-24)

## Why
After a hide kb erases the text from the database, but a plain SHA-256 of the text stayed in several identifiers. Measured on a test folder after a hide and a commit
(probe of 2026-11-24, guessing the paragraph exactly): the `fingerprint` column matched, the `unit_version_id` re-derived from the guess existed, the `statement_id`
re-derived from the guess existed, and the chunk row of the unit was still there with an id that contains a hash of the chunk text. Someone who held the database (or an
old backup) and could guess a paragraph could therefore confirm it. A keyed fingerprint alone would have closed only two of these routes, so every hash of text is keyed.

## What is keyed
Everything that is derived from text and stays in the database or in a published artifact:
* `ids.text_sha256` and `ids.fingerprint` (HMAC-SHA256 under the key, domain `kb.text.v1`): the fingerprint column, the text hash inside a chunk id, the text hashes in manifests and in the
  full-text and vector indexes;
* `ids.statement_id`: the value and the evidence enter the preimage only as one keyed digest of their canonical form (domain `kb.value.v1`; the id's own domain is `kb.st.v3`), for all statements;
* the structural key of a unit (`pipeline.unit_key`) holds a keyed digest of its heading path, never the headings;
* `pipeline.unit_output_sha256` (goes into the unit version id), the output hash of a whole conversion (`conversions.output_sha256`), and `commit.chunk_signature` (the chunk memo):
  `ids.keyed_sha256`.
The domains of the unit version, statement and chunk ids are now `kb.uv.v2`, `kb.st.v3`, `kb.ck.v2`; `tests/test_ids.py` pins one value and checks it against an independent
implementation. The id of a revision and the hash of an original file (`blobs/`) are not keyed: the original files stay by decision.

## The key
32 random bytes in `text.key`, in the data folder next to `kb.db`, **not** in `backups/`. It is made when the folder is created (migration 7) and never replaced. The database keeps
`text_key_check` (HMAC of a constant under the key) so that a missing, damaged (not 32 bytes) or foreign key is refused at connect time, with a message, before anything is written.
`ids` has no fallback: without a key every hash of text raises, it never falls back to a plain hash.

What it protects: a copy of `kb.db` alone (or a backup of the database alone) taken **without** `text.key`. What it does not: someone who has the whole data folder, key included, can still confirm a guess
(and so can any process of the owner), and the original files in `blobs/` hold the hidden text itself. **Roy copies the whole folder, so the key travels with every backup and a lost key is covered by it
(see "Decision" at the end); no separate key backup is built.** If the key is lost anyway, the way out is a new data folder with the original files added again; there is no re-keying of an existing folder.

## What it costs
* Ids are no longer the same for the same content in two data folders (they were before). Within one folder they are as stable as before.
* A data folder made before this change that already holds sources is **refused** by migration 7 with a message (ids made without a key cannot be turned into ids made with one); an
  empty one is upgraded. No real folder existed when this was decided (all end-to-end runs used synthetic or public material).
* One data folder per process: the key is a module-level value. Every `db.connect` binds the key of its own folder again, and the check value refuses a mismatch, so two folders in one
  process work as long as no id is made while the other folder's connection is the one that was bound last.
* The "same text takes its hidden id back after a rewrite" rule (seventh review) works as before and needs the key, which is the point.

## Measured after the change (`tests/test_deletion.py`, `TestNoHashOfHiddenTextLeftForAGuesser`)
After a hide and a commit: no plain SHA-256 of the paragraph, of its unit form or of the whole conversion appears in any table or in any file of the data folder except the original
files; with the right key the fingerprint, unit version id and statement id are found (so the test means something); with another key none of them is.

## Not covered
* The vector index holds embeddings, which are derived from the text; they are dropped when the index is rebuilt after a hide (earlier rounds), not checked again here.
* The original file and its plain hash stay by decision (levels a and b).
* `upgrade.py` compares plain hashes in memory only; nothing of that is stored.
* `kb doctor` does not report on the key yet.

## Ninth review (2026-11-24, 小o): what the first version of this change missed
* **Headings and offsets were plain.** Hiding a source left every heading of it readable in `unit_versions.locator_json` (inside the structural key) and `conversion_units.locator_json`
  (headings, character and line offsets); a hidden unit left its own place in the file. Measured before the fix (probe): "Zebra" and "Quagga" from the headings of a hidden source were still in
  both tables. Now the structural key holds a keyed digest of the heading path, never the headings, and `scrub_hidden_text` sets the locator of every unit version of a hidden source or unit
  to `{}` (migration 8: the locator may be rewritten only inside the scrub window, the three identity columns never). Measured after: no word of a hidden source's headings or paragraphs in any
  table or file of the folder except the original files.
* **Evidence was plain in `statement_id`.** The value was keyed, the evidence went into the preimage as it was. Value and evidence now enter as one keyed digest (domain `kb.st.v3`).
* **A check table without a row** is refused (it used to return and leave the key of another folder in use). **The key file is written whole**: a temporary file in the same folder, flushed to the disk,
  then given its name with a hard link that fails when the name is taken: absent or whole, never replaced. On a file system without hard links the final name is created exclusively and
  written at once: a crash there can leave a short file, which is refused ("damaged") and made again only by the migration that creates the key, before any data uses it.
* **Resolved by reading, not a defect:** `upgrade.py` compares hashes it computes in memory on both sides (the stored side from the stored text), never `conversions.output_sha256`; the audit log accepts
  only identifiers, digests of originals, small integers and fixed tokens (`audit._check_value`).
* **What stays and cannot be erased without breaking something else (limits, stated):** the *length* of a hidden paragraph can be read from the gap between the offsets of its visible neighbours;
  the number and order of units and the chunk rows (empty text) remain; **hiding a heading unit does not remove its text from the breadcrumb of the units below it** (they keep their own heading
  path, which is their own content); FTS tokens and vectors depend on the rebuild after a hide, as in earlier rounds. The message for a lost key said "rebuild from blobs/": kb has no command for that and
  `blobs/` names files by hash, so it now says to add the original files again.

## Backup of the key (consultation with 小o, 2026-11-24; Roy asked for a recommendation)
小o's view: option E, the lean form of A plus B: the working key lives **outside** the data folder (`KB_KEY_FILE` or `--key-file`, absolute path, explicit like `KB_HOME`), and a recovery string is kept in
the password manager; commands `kb key export`, `kb key verify`, `kb key status` (status shows source, readability, a non-secret fingerprint and whether it matches the database, never the key);
no DPAPI (not portable across a reinstall, still needs a recovery string), no passphrase (scheduled tasks and agents have nobody to type it). The recovery string must carry the whole key (Base32 without
look-alike characters in groups, plus a short check code); `verify` checks the shape and the code, compares with the key in use and with the database check value, and prints only success or failure.
`export` should refuse a non-interactive output (an agent running it would leave the key in the conversation) and take no secret from the command line. The real boundary is the **backup set and its
access rights, not the folder name**: a key file that ends up in the same cloud account as the database separates nothing. A key on the machine cannot be protected from malware running as Roy.

What I add after checking the facts: (1) **`blobs/` holds the original files**, which by decision stay after a hide at levels a and b. A backup of the whole data folder therefore contains the hidden
text itself, in clear, whatever happens to the key. The key separation only matters for a copy of `kb.db` (or its backups) that travels **without** `blobs/` (or when sources are added by reference, not by
copy). (2) So the likelier harm is the opposite one, **losing the key**: kb then refuses to open the folder. The recovery string is the first thing to have; separation is the second.
(3) kb has no backup command; `backups/` holds only the pre-migration copies and they are deleted at the next commit once anything is hidden.

**Recommendation:** (a) Roy copies the key into the password manager now (a 32-byte file: 64 hex characters, e.g. `python -c "print(open(r'<home>/text.key','rb').read().hex())"` in his own terminal, not in a
session with an agent); (b) kb gets `--key-file`/`KB_KEY_FILE` (absolute, no default), `kb key export` (interactive terminal only, or to a new file), `kb key verify`, `kb key status`; (c) no DPAPI, no
passphrase; (d) Roy tells me how the folder is backed up today (whole-folder copy, cloud sync, external disk): that single fact decides whether to move the key out of the folder at all. Not built yet:
this is a recommendation, and the commands change the interface of kb.

### Decision (2026-11-24, Roy: "the whole folder is copied")
Roy backs up by copying the whole data folder, so a backup holds `kb.db`, `text.key` **and** `blobs/` together. Consequences, and what follows:
* The key travels with every backup, so **a lost key is covered by his own backup** (the likelier harm is already handled), and moving the key out of the folder would give nothing: a copy of the folder
  holds the key, the database and the original files, which contain the hidden text itself. The keyed hashes protect a copy of `kb.db` alone (or a backup of the database alone), nothing wider.
* So **no `--key-file`, no `kb key` commands for now** (not needed for his way of working; they would change the interface of kb for no gain). Reopen the question if the backup method changes
  (database only, a cloud sync of `kb.db` without `blobs/`, a copy given to someone else).
* **After a hide, every older copy of the folder still holds the text** (database, full-text index and originals as they were). kb erases what it keeps itself (`backups/`, ask records, working folders);
  it cannot reach copies Roy made. Delete or replace the copies made before a hide when the hidden text matters.
