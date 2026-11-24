"""Schema v1 (Gate 2 section 4.2). Plain SQL strings; the migration runner splits and executes them inside one transaction.

Rules enforced by the database itself (not only by code), because a rule that lives only in code is skipped by the next code path:
  * identity-bearing rows (blobs, revisions, statements' content, approvals, tombstones, audit events) cannot be deleted or rewritten;
  * the commit member tables are frozen as soon as their commit leaves the 'staging' state;
  * at most one commit is 'active', and active_commit is a single row.
"""

V1_STATEMENTS = [
    """CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )""",
    """CREATE TABLE collections (
        collection_id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        profile_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE sources (
        source_id TEXT PRIMARY KEY,
        collection_id TEXT NOT NULL REFERENCES collections(collection_id),
        kind TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE source_locations (
        source_id TEXT NOT NULL REFERENCES sources(source_id),
        seq INTEGER NOT NULL,
        kind TEXT NOT NULL,
        value TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        PRIMARY KEY (source_id, seq)
    )""",
    """CREATE TABLE blobs (
        sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64),
        size INTEGER NOT NULL CHECK (size >= 0),
        mode TEXT NOT NULL CHECK (mode IN ('copy','ref')),
        path TEXT NOT NULL,
        first_seen TEXT NOT NULL
    )""",
    """CREATE TABLE revisions (
        revision_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES sources(source_id),
        blob_sha256 TEXT NOT NULL REFERENCES blobs(sha256)
    )""",
    """CREATE TABLE revision_observations (
        obs_id INTEGER PRIMARY KEY AUTOINCREMENT,
        revision_id TEXT NOT NULL REFERENCES revisions(revision_id),
        seen_at TEXT NOT NULL,
        parent_revision_id TEXT REFERENCES revisions(revision_id)
    )""",
    """CREATE TABLE conversions (
        conv_id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL REFERENCES revisions(revision_id),
        converter TEXT NOT NULL,
        version TEXT NOT NULL,
        params_hash TEXT NOT NULL,
        exec_identity_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('ok','partial','failed')),
        output_sha256 TEXT,
        report_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE units (
        unit_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE unit_versions (
        unit_version_id TEXT PRIMARY KEY,
        unit_id TEXT NOT NULL REFERENCES units(unit_id),
        conv_id TEXT REFERENCES conversions(conv_id),
        kind TEXT NOT NULL,
        locator_json TEXT NOT NULL,
        fingerprint TEXT NOT NULL
    )""",
    """CREATE TABLE unit_lineage (
        unit_id TEXT NOT NULL REFERENCES units(unit_id),
        parent_unit_id TEXT NOT NULL REFERENCES units(unit_id),
        op TEXT NOT NULL CHECK (op IN ('split','merge')),
        at TEXT NOT NULL
    )""",
    """CREATE TABLE statements (
        statement_id TEXT PRIMARY KEY,
        subject_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value_json TEXT NOT NULL,
        origin TEXT NOT NULL CHECK (origin IN ('native','derived','human','ai')),
        confidence REAL,
        evidence_json TEXT NOT NULL,
        supersedes TEXT REFERENCES statements(statement_id),
        state TEXT NOT NULL CHECK (state IN ('candidate','committed','rejected')),
        proposal_id TEXT
    )""",
    """CREATE INDEX statements_subject ON statements(subject_id, key)""",
    """CREATE TABLE patches (
        patch_id TEXT PRIMARY KEY,
        unit_id TEXT NOT NULL REFERENCES units(unit_id),
        anchor_json TEXT NOT NULL,
        op TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        replay_state TEXT NOT NULL CHECK (replay_state IN ('applied','needs_review','orphaned')),
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE chunks (
        chunk_id TEXT PRIMARY KEY,
        unit_version_ids_json TEXT NOT NULL,
        text TEXT NOT NULL,
        strategy TEXT NOT NULL,
        params_hash TEXT NOT NULL
    )""",
    """CREATE TABLE commits (
        commit_id TEXT PRIMARY KEY,
        parent_commit_id TEXT REFERENCES commits(commit_id),
        manifest_sha256 TEXT,
        state TEXT NOT NULL CHECK (state IN ('staging','verified','publishing','active','retired','abandoned')),
        created_at TEXT NOT NULL
    )""",
    """CREATE UNIQUE INDEX commits_one_active ON commits(state) WHERE state = 'active'""",
    """CREATE TABLE commit_units (
        commit_id TEXT NOT NULL REFERENCES commits(commit_id),
        unit_version_id TEXT NOT NULL REFERENCES unit_versions(unit_version_id),
        PRIMARY KEY (commit_id, unit_version_id)
    )""",
    """CREATE TABLE commit_statements (
        commit_id TEXT NOT NULL REFERENCES commits(commit_id),
        statement_id TEXT NOT NULL REFERENCES statements(statement_id),
        role TEXT NOT NULL CHECK (role IN ('display','alternate')),
        PRIMARY KEY (commit_id, statement_id)
    )""",
    """CREATE TABLE commit_chunks (
        commit_id TEXT NOT NULL REFERENCES commits(commit_id),
        chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
        PRIMARY KEY (commit_id, chunk_id)
    )""",
    """CREATE TABLE active_commit (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        commit_id TEXT NOT NULL REFERENCES commits(commit_id)
    )""",
    """CREATE TABLE proposals (
        proposal_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('ai_edit','metadata','merge_tag','delete','purge','patch')),
        diff_json TEXT NOT NULL,
        requested_by TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending','approved','rejected','applied','stale')),
        base_commit_id TEXT REFERENCES commits(commit_id),
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE approvals (
        approval_id TEXT PRIMARY KEY,
        proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
        approved_by TEXT NOT NULL,
        at TEXT NOT NULL,
        override_protected INTEGER NOT NULL DEFAULT 0 CHECK (override_protected IN (0,1))
    )""",
    """CREATE TABLE tombstones (
        tombstone_id TEXT PRIMARY KEY,
        target_id TEXT NOT NULL,
        level TEXT NOT NULL CHECK (level IN ('a','b','c')),
        proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
        at TEXT NOT NULL
    )""",
    """CREATE TABLE audit_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        type TEXT NOT NULL,
        ids_json TEXT NOT NULL
    )""",
]

# Rows that carry identity or history are append-only.
_NO_DELETE = ["blobs", "revisions", "statements", "approvals", "tombstones", "audit_events", "sources", "source_locations",
              "revision_observations", "conversions", "unit_versions", "units"]
# sources and units are identity rows too: a source cannot be re-homed to another collection and a unit's creation time cannot be rewritten.
# If moving a source between collections is ever wanted, that is a new migration with an explicit rule, not a silent UPDATE.
_NO_UPDATE = ["blobs", "revisions", "approvals", "tombstones", "audit_events", "revision_observations", "conversions",
              "unit_versions", "source_locations", "sources", "units"]

V1_TRIGGERS = []
for _t in _NO_DELETE:
    V1_TRIGGERS.append(f"""CREATE TRIGGER {_t}_no_delete BEFORE DELETE ON {_t}
        BEGIN SELECT RAISE(ABORT, '{_t} rows are never deleted'); END""")
for _t in _NO_UPDATE:
    V1_TRIGGERS.append(f"""CREATE TRIGGER {_t}_no_update BEFORE UPDATE ON {_t}
        BEGIN SELECT RAISE(ABORT, '{_t} rows are never rewritten'); END""")

# A statement is immutable except for its lifecycle fields (state, proposal_id): its identity is a hash of the rest.
V1_TRIGGERS.append("""CREATE TRIGGER statements_content_frozen BEFORE UPDATE OF statement_id, subject_id, key, value_json, origin,
        evidence_json, supersedes, confidence ON statements
    BEGIN SELECT RAISE(ABORT, 'a statement is corrected by a new statement that supersedes it'); END""")

# Commit membership can only change while the commit is being assembled.
def _not_staging(ref):
    return f"(SELECT state FROM commits WHERE commit_id = {ref}.commit_id) IS NOT 'staging'"


for _t in ("commit_units", "commit_statements", "commit_chunks"):
    # an UPDATE can move a row between commits, so BOTH the commit it leaves and the commit it joins must be in staging
    for _ev, _cond in (("INSERT", _not_staging("NEW")), ("DELETE", _not_staging("OLD")),
                       ("UPDATE", _not_staging("OLD") + " OR " + _not_staging("NEW"))):
        V1_TRIGGERS.append(f"""CREATE TRIGGER {_t}_frozen_{_ev.lower()} BEFORE {_ev} ON {_t}
            WHEN {_cond}
            BEGIN SELECT RAISE(ABORT, 'commit members are frozen once the commit leaves staging'); END""")

V1_STATEMENTS = V1_STATEMENTS + V1_TRIGGERS

# Migration 3: which unit versions make up a conversion, in document order, and WHERE in that revision each one sits. A unit version is the
# same across revisions while its text and structural key are the same, so character offsets (which shift when text before them changes)
# belong here, per conversion, and not in the unit version's identity.
V3_STATEMENTS = [
    """CREATE TABLE conversion_units (
        conv_id TEXT NOT NULL REFERENCES conversions(conv_id),
        ordinal INTEGER NOT NULL,
        unit_version_id TEXT NOT NULL REFERENCES unit_versions(unit_version_id),
        locator_json TEXT NOT NULL,
        PRIMARY KEY (conv_id, ordinal)
    )""",
    """CREATE TRIGGER conversion_units_no_update BEFORE UPDATE ON conversion_units
        BEGIN SELECT RAISE(ABORT, 'conversion_units rows are never rewritten'); END""",
    """CREATE TRIGGER conversion_units_no_delete BEFORE DELETE ON conversion_units
        BEGIN SELECT RAISE(ABORT, 'conversion_units rows are never deleted'); END""",
]

# Migration 4 (stage S2): the review rules the database itself enforces, so that no code path (including a future one) can skip them.
#   * a tombstone exists only for an approved delete proposal THAT NAMES THAT TARGET AND LEVEL (T-D1); level c does not exist yet;
#   * a proposal is approved only while it is pending, its state moves along legal paths only, and what was approved cannot be edited afterwards
#     (its identity columns are frozen, its difference can only be blanked once decided);
#   * a human or AI statement becomes 'committed' only with an approved proposal of a fitting kind (ai -> ai_edit; human -> metadata or patch)
#     that the statement itself names in its evidence;
#   * decided_at lets decided proposals expire (their diffs may hold original text and are kept for a limited time only);
#   * patches remember which approved proposal they came from;
#   * chunk_memo: derived and re-creatable, it lets a commit re-chunk only the sources whose input changed (T-INC1).
# What these triggers are for: a bug or a careless future code path in kb cannot skip the review rules. They are not a defence against someone who
# can write the database file directly (that person can drop the triggers too).
_APPROVED = ("EXISTS (SELECT 1 FROM proposals p JOIN approvals a ON a.proposal_id = p.proposal_id "
             "WHERE p.proposal_id = NEW.proposal_id AND p.state IN ('approved','applied') "
             "AND json_extract(NEW.evidence_json, '$.proposal_id') = NEW.proposal_id "
             "AND ((NEW.origin = 'ai' AND p.kind = 'ai_edit') OR (NEW.origin = 'human' AND p.kind IN ('metadata','patch'))))")
V4_STATEMENTS = [
    "ALTER TABLE proposals ADD COLUMN decided_at TEXT",
    "ALTER TABLE patches ADD COLUMN proposal_id TEXT REFERENCES proposals(proposal_id)",
    """CREATE TABLE chunk_memo (
        signature TEXT PRIMARY KEY,
        chunk_ids_json TEXT NOT NULL
    )""",
    """CREATE TRIGGER tombstones_need_approval BEFORE INSERT ON tombstones
        WHEN NEW.level NOT IN ('a','b') OR NOT EXISTS (
            SELECT 1 FROM proposals p JOIN approvals a ON a.proposal_id = p.proposal_id
            WHERE p.proposal_id = NEW.proposal_id AND p.kind = 'delete' AND p.state IN ('approved','applied')
            AND json_extract(p.diff_json, '$.target_id') = NEW.target_id AND json_extract(p.diff_json, '$.level') = NEW.level)
        BEGIN SELECT RAISE(ABORT, 'a tombstone needs an approved delete proposal for this target and level'); END""",
    """CREATE TRIGGER statements_proposal_link_frozen BEFORE UPDATE OF proposal_id ON statements
        WHEN OLD.proposal_id IS NOT NEW.proposal_id AND (OLD.proposal_id IS NOT NULL OR OLD.state = 'committed')
        BEGIN SELECT RAISE(ABORT, 'the proposal a statement came from is never changed afterwards'); END""",
    """CREATE TRIGGER proposals_identity_frozen BEFORE UPDATE OF proposal_id, kind, requested_by, base_commit_id, created_at ON proposals
        BEGIN SELECT RAISE(ABORT, 'what a proposal is, who asked and against which commit are never rewritten'); END""",
    """CREATE TRIGGER proposals_diff_frozen BEFORE UPDATE OF diff_json ON proposals
        WHEN OLD.diff_json != NEW.diff_json AND NOT (NEW.diff_json = '{"purged": true}' AND OLD.state IN ('rejected','applied','stale'))
        BEGIN SELECT RAISE(ABORT, 'a proposal difference is never edited; a decided one can only be blanked'); END""",
    """CREATE TRIGGER approvals_only_pending BEFORE INSERT ON approvals
        WHEN (SELECT state FROM proposals WHERE proposal_id = NEW.proposal_id) IS NOT 'pending'
        BEGIN SELECT RAISE(ABORT, 'only a pending proposal can be approved'); END""",
    """CREATE TRIGGER proposals_state_machine BEFORE UPDATE OF state ON proposals
        WHEN OLD.state != NEW.state AND NOT (
            (OLD.state = 'pending' AND NEW.state IN ('approved','rejected','stale')) OR
            (OLD.state = 'approved' AND NEW.state IN ('applied','stale')))
        BEGIN SELECT RAISE(ABORT, 'illegal proposal state change'); END""",
    f"""CREATE TRIGGER statements_need_approval_update BEFORE UPDATE OF state ON statements
        WHEN NEW.state = 'committed' AND NEW.origin IN ('human','ai') AND NOT {_APPROVED}
        BEGIN SELECT RAISE(ABORT, 'a human or AI statement is committed only with an approved proposal of a fitting kind'); END""",
    f"""CREATE TRIGGER statements_need_approval_insert BEFORE INSERT ON statements
        WHEN NEW.state = 'committed' AND NEW.origin IN ('human','ai') AND NOT {_APPROVED}
        BEGIN SELECT RAISE(ABORT, 'a human or AI statement is committed only with an approved proposal of a fitting kind'); END""",
]

# Migration 2: what the intake refused, deferred or could not read. One current row per (collection, path); a later successful
# intake of the path removes its row.
V2_STATEMENTS = [
    """CREATE TABLE intake_skips (
        collection_id TEXT NOT NULL REFERENCES collections(collection_id),
        path TEXT NOT NULL,
        reason TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        PRIMARY KEY (collection_id, path)
    )""",
]


# Migration 5: finding a conversion unit by its unit version. A search hit needs the position of its first unit; without this index that lookup
# scans every row of conversion_units (about 113 ms per hit on a home of 47,694 chunks, found by the S3 midterm latency measurement).
V5_STATEMENTS = [
    """CREATE INDEX conversion_units_by_unit_version ON conversion_units(unit_version_id)""",
]


# Migration 6: the text of what has been hidden can be erased from the derived rows (Gate 1 H2). A statement is immutable (its identity is a hash of its content);
# the only way to change one is inside an explicit scrub window: a row in scrub_window, present only inside the single transaction of commit.scrub_hidden_text.
V6_STATEMENTS = [
    """CREATE TABLE scrub_window (id INTEGER PRIMARY KEY CHECK (id = 1))""",
    """DROP TRIGGER statements_content_frozen""",
    """CREATE TRIGGER statements_content_frozen BEFORE UPDATE OF statement_id, subject_id, key, value_json, origin,
        evidence_json, supersedes, confidence ON statements
    WHEN NOT EXISTS (SELECT 1 FROM scrub_window)
    BEGIN SELECT RAISE(ABORT, 'a statement is corrected by a new statement that supersedes it'); END""",
]

# Migration 7: every hash of text is made with a key (kb.keys); the database keeps a check value to tell its own key from another one (stage S4, eighth review).
V7_STATEMENTS = [
    """CREATE TABLE text_key_check (id INTEGER PRIMARY KEY CHECK (id = 1), check_value TEXT NOT NULL)""",
]

# Migration 8: the locator of a hidden unit (headings, character and line offsets) is erased with the rest of its text, inside the scrub window (ninth review).
# What is never rewritten stays that way: the three identity columns of a conversion unit.
V8_STATEMENTS = [
    """DROP TRIGGER conversion_units_no_update""",
    """CREATE TRIGGER conversion_units_identity_frozen BEFORE UPDATE OF conv_id, ordinal, unit_version_id ON conversion_units
        BEGIN SELECT RAISE(ABORT, 'conversion_units rows are never rewritten'); END""",
    """CREATE TRIGGER conversion_units_locator_frozen BEFORE UPDATE OF locator_json ON conversion_units
        WHEN NOT EXISTS (SELECT 1 FROM scrub_window)
        BEGIN SELECT RAISE(ABORT, 'conversion_units rows are never rewritten'); END""",
]
