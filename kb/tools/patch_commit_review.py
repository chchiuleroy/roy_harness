"""One-off patch of src/kb/commit.py after review of S1 (小o): every replacement must match exactly once."""

P = "C:/Users/roy/kb/src/kb/commit.py"
with open(P, encoding="utf-8", newline="") as f:
    s = f.read()


def rep(old, new):
    global s
    assert s.count(old) == 1, old[:80]
    s = s.replace(old, new)


# --- approvals and visible link targets reach the display rule
rep('def _subject_statements(conn, subject_id, conv_id, roles, only_key=None, skip_key=None):',
    'def approval_sets(conn):\n'
    '    """Statement ids that carry an approval (statement -> proposal -> approval), and those whose approval had override_protected."""\n'
    '    approved, override = set(), set()\n'
    '    for sid, flag in conn.execute(\n'
    '            "SELECT s.statement_id, max(a.override_protected) FROM statements s JOIN proposals p ON p.proposal_id = s.proposal_id "\n'
    '            "JOIN approvals a ON a.proposal_id = p.proposal_id WHERE p.state IN (\'approved\', \'applied\') GROUP BY s.statement_id"):\n'
    '        approved.add(sid)\n'
    '        if flag:\n'
    '            override.add(sid)\n'
    '    return frozenset(approved), frozenset(override)\n'
    '\n\n'
    'def _subject_statements(conn, subject_id, conv_id, roles, only_key=None, skip_key=None, approved=frozenset(), override_ok=frozenset(),\n'
    '                        visible_sources=None):')
rep('''        if (only_key and key != only_key) or (skip_key and key == skip_key):
            continue''',
    '''        if (only_key and key != only_key) or (skip_key and key == skip_key):
            continue
        if key == "links_to_source" and visible_sources is not None and json.loads(value_json)["ref"] not in visible_sources:
            continue                                       # an edge to a hidden or removed source must not survive in a commit''')
rep('        shown, _alts = display.choose(items)', '        shown, _alts = display.choose(items, approved, override_ok)')
rep('''    seen_conv = {}
    all_uv, stmt_roles = set(), {}
    for source_id, collection_id in included_sources(conn):
        asm.sources += 1
        conv = current_conversion(conn, source_id)
        if conv is None:
            _subject_statements(conn, source_id, None, stmt_roles)
            continue
        conv_id, cname, cver, cparams, revision_id = conv
        _subject_statements(conn, source_id, conv_id, stmt_roles)''',
    '''    seen_conv = {}
    all_uv, stmt_roles = set(), {}
    approved, override_ok = approval_sets(conn)
    included = included_sources(conn)
    visible = {sid for sid, _ in included}
    ctx = {"approved": approved, "override_ok": override_ok, "visible_sources": visible}
    for source_id, collection_id in included:
        asm.sources += 1
        conv = current_conversion(conn, source_id)
        if conv is None:
            _subject_statements(conn, source_id, None, stmt_roles, **ctx)
            continue
        conv_id, cname, cver, cparams, revision_id = conv
        _subject_statements(conn, source_id, conv_id, stmt_roles, **ctx)''')
rep('            shown = _subject_statements(conn, uv, conv_id, stmt_roles)', '            shown = _subject_statements(conn, uv, conv_id, stmt_roles, **ctx)')

# --- manifest records statement roles, not only ids
rep('"statements": sorted(asm.statements), "chunks": sorted(c.chunk_id for c in asm.chunks)},',
    '"statements": dict(sorted(asm.statements.items())), "chunks": sorted(c.chunk_id for c in asm.chunks)},')

# --- ghost check: membership, roles, commitment and chunk payloads
rep('''    if manifest["index"]["tokenizer"] != tk.TOKENIZER_ID:
        problems.append("tokenizer version differs")''',
    '''    # membership: everything the manifest promises about units, statements and roles must be what the database holds
    db_units = sorted(r[0] for r in conn.execute("SELECT unit_version_id FROM commit_units WHERE commit_id = ?", (commit_id,)))
    db_roles = {sid: role for sid, role in conn.execute("SELECT statement_id, role FROM commit_statements WHERE commit_id = ?", (commit_id,))}
    if db_units != manifest["members"]["unit_versions"]:
        problems.append("unit versions differ between the database and the manifest")
    if db_roles != manifest["members"]["statements"]:
        problems.append("statements or their display roles differ between the database and the manifest")
    recomputed = hashlib.sha256(canonical_bytes({
        "units": db_units, "statements": sorted(db_roles.items()), "chunks": sorted(expected), "params": manifest["index"]["params_hash"],
        "tokenizer": manifest["index"]["tokenizer"], "visibility": manifest["visibility_input_sha256"], "format": manifest["format"]})).hexdigest()
    if recomputed != manifest["members_sha256"]:
        problems.append("members commitment differs from what the database holds")
    # every chunk's id must be reproducible from its own members, text and the committed parameters
    name, _, version = manifest["index"]["chunker"].partition("@")
    for chunk_id, uvs_json, text in conn.execute(
            "SELECT c.chunk_id, c.unit_version_ids_json, c.text FROM commit_chunks cc JOIN chunks c ON c.chunk_id = cc.chunk_id WHERE cc.commit_id = ?",
            (commit_id,)):
        try:
            again = ids.chunk_id(json.loads(uvs_json), name, version, manifest["index"]["params_hash"], ids.text_sha256(text))
        except KbError:
            again = None
        if again != chunk_id:
            problems.append(f"chunk payload does not match its id for {chunk_id}")
    if manifest["index"]["tokenizer"] != tk.TOKENIZER_ID:
        problems.append("tokenizer version differs")''')

# --- activation re-checks the visibility inputs inside the transaction
rep('''def _activate(conn, commit_id, manifest_sha, member_statements):
    conn.execute("BEGIN IMMEDIATE")
    try:
        old = active_commit_id(conn)''',
    '''def _activate(conn, commit_id, manifest_sha, member_statements, visibility_sha):
    conn.execute("BEGIN IMMEDIATE")
    try:
        if visibility_sha != visibility_input_sha256(conn):
            raise CommitFailed("a profile or tombstone changed after this commit was assembled; it was not activated")
        old = active_commit_id(conn)''')
rep('    _activate(conn, commit_id, manifest_sha, member_statements)\n    fault_point("commit.after_activate")',
    '    _activate(conn, commit_id, manifest_sha, member_statements, manifest["visibility_input_sha256"])\n    fault_point("commit.after_activate")')

# --- recovery treats a publishing commit like a new one: full verification before activation
rep('''                _, problems = verify_artifacts(place, msha)
                if not problems:
                    try:
                        if place == sdir:''',
    '''                manifest, problems = verify_artifacts(place, msha)
                if not problems:
                    problems = ghost_check(conn, commit_id, place, manifest)        # content, membership and current visibility rules
                if not problems:
                    try:
                        if place == sdir:''')
rep('                        _activate(conn, commit_id, msha, _member_statements(conn, commit_id))',
    '                        _activate(conn, commit_id, msha, _member_statements(conn, commit_id), manifest["visibility_input_sha256"])')

with open(P, "w", encoding="utf-8", newline="") as f:
    f.write(s)
print("commit.py patched")
