"""One-off: make tests/test_db.py and tests/test_doctor_cli.py independent of the number of real migrations."""


def patch(path, pairs):
    with open(path, encoding="utf-8", newline="") as f:
        s = f.read()
    for old, new, count in pairs:
        assert s.count(old) == count, (path, old[:70], s.count(old))
        s = s.replace(old, new)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(s)


patch("C:/Users/roy/kb/tests/test_db.py", [
    ('                   "audit_events"}', '                   "audit_events", "intake_skips"}', 1),
    ('def tables(conn):', 'LATEST = db.MIGRATIONS[-1].version        # the real schema moves on; tests derive version numbers from it\nNEXT = LATEST + 1\n\n\ndef tables(conn):', 1),
    ('fault_point("migrate.2.mid")', 'fault_point(f"migrate.{NEXT}.mid")', 1),
    ('db.Migration(2, "note", m2_good)', 'db.Migration(NEXT, "note", m2_good)', 1),
    ('db.Migration(2, "note_bad", m2_bad)', 'db.Migration(NEXT, "note_bad", m2_bad)', 1),
    ('self.assertEqual(db.schema_version(conn), 1)', 'self.assertEqual(db.schema_version(conn), LATEST)', 3),
    ('self.assertEqual(db.migrate(path, M2_GOOD), [2])', 'self.assertEqual(db.migrate(path, M2_GOOD), [NEXT])', 1),
    ('self.assertEqual(db.schema_version(conn), 2)', 'self.assertEqual(db.schema_version(conn), NEXT)', 1),
    ('self.assertEqual(db.schema_version(db.connect(path)), 2)', 'self.assertEqual(db.schema_version(db.connect(path)), NEXT)', 1),
    ('self.assertEqual(h.init(), [1])', 'self.assertEqual(h.init(), list(range(1, LATEST + 1)))', 1),
    ('self._kill_during("migrate.2.mid", "M2_GOOD", seeded=True)', 'self._kill_during(f"migrate.{NEXT}.mid", "M2_GOOD", seeded=True)', 1),
])
patch("C:/Users/roy/kb/tests/test_doctor_cli.py", [
    ('self.assertIn("[1]", out)', 'self.assertIn(str(list(range(1, db.MIGRATIONS[-1].version + 1))), out)', 1),
    ('self.assertIn("schema_version: 1", out)', 'self.assertIn(f"schema_version: {db.MIGRATIONS[-1].version}", out)', 1),
])
print("patched")
