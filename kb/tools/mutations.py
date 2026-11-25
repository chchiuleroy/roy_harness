"""Mutation list: (name, file under src/kb or tools/..., old text, new text). Add entries together with the feature they guard."""

MUTATIONS = [
    # ---- ids / canonical (T-ID1)
    ("ids_drop_domain", "ids.py", "return hashlib.sha256(canonical_bytes([domain] + list(fields))).digest()",
     "return hashlib.sha256(canonical_bytes(list(fields))).digest()"),
    ("ids_concat_instead_of_structure", "ids.py", "return hashlib.sha256(canonical_bytes([domain] + list(fields))).digest()",
     "return hashlib.sha256((domain + ''.join(str(f) for f in fields)).encode('utf-8')).digest()"),
    ("ids_chunk_unsorted", "ids.py", "uvs = sorted({_need_id(u, \"unit_version_id\", {\"uv\"}) for u in unit_version_ids})",
     "uvs = [_need_id(u, \"unit_version_id\", {\"uv\"}) for u in unit_version_ids]"),
    ("ids_chunk_not_deduplicated", "ids.py", "uvs = sorted({_need_id(u, \"unit_version_id\", {\"uv\"}) for u in unit_version_ids})",
     "uvs = sorted([_need_id(u, \"unit_version_id\", {\"uv\"}) for u in unit_version_ids])"),
    ("ids_prefix_not_checked", "ids.py", "not ID_RE.match(value) or value.split(\"_\", 1)[0] not in prefixes:", "not ID_RE.match(value):"),
    ("ids_chunk_drop_params_hash", "ids.py", "chunker_version, _need_hex(params_hash, \"params_hash\"),\n                                  _need_hex(text_hash, \"text_hash\")])",
     "chunker_version, _need_hex(text_hash, \"text_hash\")])"),
    ("ids_revision_drop_source", "ids.py", "[_need_id(source_id, \"source_id\", {\"src\"}), _need_hex(blob_sha256, \"blob_sha256\")])",
     "[_need_hex(blob_sha256, \"blob_sha256\")])"),
    ("canonical_no_nfc", "canonical.py", "return unicodedata.normalize(\"NFC\", text)", "return text"),
    ("canonical_allow_float", "canonical.py", "    raise CanonicalError(f\"{type(obj).__name__} is not allowed in a canonical preimage\")",
     "    return obj"),
    ("canonical_unsorted", "canonical.py", "sort_keys=True", "sort_keys=False"),
    ("ids_new_id_weak", "ids.py", "secrets.token_bytes(15)", "b'0' * 15"),
    # ---- db / schema / migrations / writer (T-M1..M4, T-W1)
    ("db_backup_check_removed", "db.py", "            if not integrity_ok(dest):", "            if False:"),
    ("db_downgrade_allowed", "db.py", "        if current > latest:", "        if False:"),
    ("db_backup_skipped", "db.py", "        if current > 0:\n            backup_dir", "        if False:\n            backup_dir"),
    ("db_gap_check_removed", "db.py", "    if [m.version for m in migrations] != list(range(1, latest + 1)):", "    if False:"),
    ("db_user_version_never_set", "db.py", "                conn.execute(f\"PRAGMA user_version={int(m.version)}\")", "                pass"),
    ("db_fk_off", "db.py", "    conn.execute(\"PRAGMA foreign_keys=ON\")", "    conn.execute(\"PRAGMA foreign_keys=OFF\")"),
    ("db_no_wal", "db.py", "        conn.execute(\"PRAGMA journal_mode=WAL\")\n", "        pass\n"),
    ("db_restore_unchecked", "db.py", "    if not integrity_ok(backup_path):\n        raise BackupInvalid(f\"backup {backup_path} failed", "    if False:\n        raise BackupInvalid(f\"backup {backup_path} failed"),
    ("schema_blobs_deletable", "schema.py", "_NO_DELETE = [\"blobs\", ", "_NO_DELETE = ["),
    ("schema_audit_rewritable", "schema.py", "_NO_UPDATE = [\"blobs\", \"revisions\", \"approvals\", \"tombstones\", \"audit_events\", ", "_NO_UPDATE = [\"blobs\", \"revisions\", \"approvals\", \"tombstones\", "),
    ("schema_sources_and_units_rewritable", "schema.py", "\"unit_versions\", \"source_locations\", \"sources\", \"units\"]", "\"unit_versions\", \"source_locations\"]"),
    ("schema_statement_content_unfrozen", "schema.py", "UPDATE OF statement_id, subject_id, key, value_json, origin,\n        evidence_json, supersedes, confidence ON statements\n    WHEN NOT EXISTS (SELECT 1 FROM scrub_window)", "UPDATE OF statement_id ON statements\n    WHEN NOT EXISTS (SELECT 1 FROM scrub_window)"),
    ("schema_commit_members_never_frozen", "schema.py", "            WHEN {_cond}\n", "            WHEN 0\n"),
    ("schema_commit_update_checks_new_only", "schema.py", "(\"UPDATE\", _not_staging(\"OLD\") + \" OR \" + _not_staging(\"NEW\"))", "(\"UPDATE\", _not_staging(\"NEW\"))"),
    ("schema_many_active_commits", "schema.py", "CREATE UNIQUE INDEX commits_one_active", "CREATE INDEX commits_one_active"),
    ("writer_never_busy", "writer.py", "            raise WriterBusy(f\"another process holds the kb writer lock ({self._holder()})\") from None", "            pass"),
    # ---- cas / audit / profile (P1, P5, P7)
    ("cas_blob_writable", "cas.py", "                os.chmod(final, stat.S_IREAD)\n                fsync_dir(fan_out)\n        except", "                fsync_dir(fan_out)\n        except"),
    ("cas_no_directory_sync", "cas.py", "                fsync_dir(fan_out)\n        except", "                pass\n        except"),
    ("cas_dedupe_ignores_content", "cas.py", "if hash_file(final) == (sha, size):", "if True:"),
    ("cas_verify_never_mismatch", "cas.py", "        self._seen.pop(sha256, None)\n        return MISMATCH", "        return OK"),
    ("cas_verify_ignores_missing", "cas.py", "        if not os.path.isfile(path):\n            self._seen.pop(sha256, None)\n            return MISSING", "        if not os.path.isfile(path):\n            return OK"),
    ("cas_has_delete", "cas.py", "    def recover(self):", "    def delete_blob(self, sha):\n        os.remove(self.blob_path(sha))\n\n    def recover(self):"),
    ("cas_recover_noop", "cas.py", "            _discard_temp(os.path.join(self.tmp, name))\n            n += 1", "            n += 1"),
    ("cas_row_before_file", "cas.py", "            fault_point(\"cas.after_temp_write\")\n            sha = h.hexdigest()", "            sha = h.hexdigest()\n            self._record(conn, sha, size, 'copy', 'x')\n            fault_point(\"cas.after_temp_write\")"),
    ("audit_accepts_text", "audit.py", "    raise AuditError(f\"{key}: only identifiers, sha256 digests, small integers and fixed tokens may be logged\")", "    return"),
    ("audit_unknown_event_ok", "audit.py", "    if event_type not in EVENT_TYPES:\n        raise AuditError(f\"unknown audit event type {event_type!r}\")", "    pass"),
    ("audit_falsey_ids_become_empty", "audit.py", "    if not isinstance(ids, dict):", "    if False:"),
    ("profile_secret_loosenable", "profile.py", "        if self.sensitivity == \"secret\" and (self.index_plaintext or self.allow_external_model or self.agent_visible):", "        if False:"),
    ("profile_unknown_keys_ignored", "profile.py", "        if unknown:\n            raise KbError(f\"unknown profile key(s) {sorted(unknown)}\")", "        d = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}"),
    ("profile_string_converters", "profile.py", "            if not isinstance(conv, list):                      # tuple(\"pdf\") would silently become ('p','d','f')\n                raise KbError(\"allowed_converters must be null or a list of converter names\")\n", ""),
    ("profile_agent_default_open", "profile.py", "    agent_visible: bool = False", "    agent_visible: bool = True"),
    ("profile_require_agent_noop", "profile.py", "    if not profile.agent_visible:\n        raise PolicyViolation(\"this collection is not visible to agents\")", "    pass"),
    ("profile_require_index_noop", "profile.py", "    if not profile.index_plaintext:\n        raise PolicyViolation(\"this collection must not be indexed in plaintext\")", "    pass"),
    ("profile_require_external_noop", "profile.py", "    if not profile.allow_external_model:\n        raise PolicyViolation(\"this collection must not be sent to an external model\")", "    pass"),
    ("profile_require_convert_noop", "profile.py", "    if profile.allowed_converters is not None and converter_name not in profile.allowed_converters:\n        raise PolicyViolation(f\"converter {converter_name!r} is not allowed for this collection\")", "    pass"),
    # ---- doctor / cli / layers (R1, OPT6)
    ("layers_allow_upward_import", "layers.py", "    return LEVEL[dst_tier] < LEVEL[src_tier]", "    return True"),
    ("layers_siblings_allowed", "layers.py", "\"T1\": 1, \"T2\": 1", "\"T1\": 1, \"T2\": 0"),
    ("graph_checker_ignores_dynamic", "tools/importgraph.py", "            if name in DYNAMIC_IMPORT_CALLS:\n                dynamic.update(strings)", "            if False:\n                dynamic.update(strings)"),
    ("graph_checker_ignores_aliases", "tools/importgraph.py", "            name = _resolve(_dotted(node.func), aliases)", "            name = _dotted(node.func)"),
    ("graph_checker_ignores_nonconstant_import", "tools/importgraph.py", "                    flags.add(\"dynamic_nonconstant\")", "                    pass"),
    ("graph_checker_ignores_exec", "tools/importgraph.py", "                flags.add(\"dynamic_code\")", "                pass"),
    ("doctor_deep_skips_blobs", "doctor.py", "        if deep and v == db.MIGRATIONS[-1].version:", "        if False:"),
    ("doctor_hides_leftover_temp", "doctor.py", "        if os.path.isdir(tmp) and os.listdir(tmp):", "        if False:"),
    ("doctor_hides_old_schema", "doctor.py", "        if v != db.MIGRATIONS[-1].version:\n            problems.append((\"schema_version\"", "        if False:\n            problems.append((\"schema_version\""),
    ("doctor_missing_cap_raises", "doctor.py", "        Capability(\"numpy\", find_spec(\"numpy\") is not None,", "        Capability(\"numpy\", find_spec(\"numpy\").origin is not None,"),
    ("cli_doctor_always_zero", "cli.py", "            return 0 if rep[\"healthy\"] else 1", "            return 0"),
    ("cli_guess_home", "home.py", "    path = arg or os.environ.get(\"KB_HOME\")", "    path = arg or os.environ.get(\"KB_HOME\") or os.getcwd()"),
    ("home_default_location", "home.py", "        raise KbError(\"no data folder: pass --home <folder> or set KB_HOME (kb has no default location)\")", "        return Home(os.getcwd())"),
    # ---- attach / detach (R3, R4, R7)
    # ---- R2 probe blind-spot coverage
    ("probe_ignores_threads", "tools/r2_probe.py", "    \"new_threads\": sorted({t.name for t in threading.enumerate()} - threads_before),", "    \"new_threads\": [],"),
    ("probe_ignores_environment", "tools/r2_probe.py", "    \"env_changed\": sorted(k for k in set(os.environ) | set(env_before) if os.environ.get(k) != env_before.get(k)),", "    \"env_changed\": [],"),
    ("probe_ignores_signals", "tools/r2_probe.py", "    \"signal_handlers_changed\": sorted(str(s) for s in signals_before if repr(signal.getsignal(s)) != signals_before[s]),", "    \"signal_handlers_changed\": [],"),
    ("probe_ignores_connects", "tools/r2_probe.py", "\"connect\": rec[\"connect\"],", "\"connect\": [],"),
]

from mutations_s1 import S1_MUTATIONS  # noqa: E402
from mutations_s2 import S2_MUTATIONS  # noqa: E402
from mutations_s3 import S3_MUTATIONS  # noqa: E402
from mutations_s4 import S4_MUTATIONS  # noqa: E402

MUTATIONS = MUTATIONS + S1_MUTATIONS + S2_MUTATIONS + S3_MUTATIONS + S4_MUTATIONS
