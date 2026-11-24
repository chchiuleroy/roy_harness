"""S4: `kb ask`, `kb answers` and the profile flags of `kb add` through the real command line, with a stand-in for the harness process."""
import io
import json
import os
import textwrap
import unittest
from contextlib import redirect_stderr

from kb import cli

from .support import tmpdir

STAND_IN = textwrap.dedent('''
    import json, os, sys
    here = os.path.dirname(os.path.abspath(__file__))
    print(json.dumps({"kind": "ready"}), flush=True)
    sys.stdin.readline()
    print(json.dumps({"kind": "turn_start"}), flush=True)
    print(json.dumps({"kind": "assistant", "text": open(os.path.join(here, "reply.txt"), encoding="utf-8").read()}), flush=True)
    print(json.dumps({"kind": "turn_end"}), flush=True)
    for line in sys.stdin:
        if json.loads(line).get("text") == "/quit":
            break
''')
GOOD = json.dumps({"answer": "The falcon flew over the harbor.", "claims": [{"text": "where", "passage": "P1", "quote": "falcon flew over the harbor at dawn"}]})
BAD = json.dumps({"answer": "It flew over the sea.", "claims": [{"text": "where", "passage": "P1", "quote": "the falcon flew over the open sea at night"}]})


class CliAsk(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir(self)
        self.home = os.path.join(self.d, "h")
        self.src = os.path.join(self.d, "src")
        os.makedirs(self.src)
        self.harness = os.path.join(self.d, "fake", "src", "harness", "stand_in.py")
        os.makedirs(os.path.dirname(self.harness))
        with open(self.harness, "w", encoding="utf-8") as f:
            f.write(STAND_IN)
        self.run_cli("init")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(["--home", self.home, *argv], out=out)
        return code, out.getvalue(), err.getvalue()

    def reply(self, text):
        with open(os.path.join(os.path.dirname(self.harness), "reply.txt"), "w", encoding="utf-8") as f:
            f.write(text)

    def doc(self, name, text, *flags, collection="open"):
        with open(os.path.join(self.src, name), "w", encoding="utf-8") as f:
            f.write(text)
        code, out, err = self.run_cli("add", os.path.join(self.src, name), "--settle", "0", "--collection", collection, *flags)
        self.assertEqual(code, 0, out + err)

    def ready_home(self):
        self.doc("falcon.md", "# Falcon\n\nThe falcon flew over the harbor at dawn, and the tide was low.\n", "--agent-visible")
        self.assertEqual(self.run_cli("commit")[0], 0)

    def ask(self, *extra, question="where did the falcon fly?"):
        return self.run_cli("ask", question, "--base-url", "http://127.0.0.1:9/v1", "--model", "m", "--harness", self.harness, "--timeout", "30", *extra)


class TestProfileFlags(CliAsk):
    def profile_of(self, name):
        from kb import db, colls
        from kb.home import Home
        conn = db.connect(Home(self.home).db_path, readonly=True)
        try:
            cid = conn.execute("SELECT collection_id FROM collections WHERE name=?", (name,)).fetchone()[0]
            return colls.get_profile(conn, cid)
        finally:
            conn.close()

    def test_a_new_collection_gets_the_profile_that_was_asked_for_and_the_default_stays_closed(self):
        self.doc("a.md", "# A\n\nfirst\n", "--agent-visible", "--allow-external-model", collection="open")
        self.doc("b.md", "# B\n\nsecond\n", collection="closed")
        opened, closed = self.profile_of("open"), self.profile_of("closed")
        self.assertEqual((opened.agent_visible, opened.allow_external_model), (True, True))
        self.assertEqual((closed.agent_visible, closed.allow_external_model), (False, False))

    def test_the_profile_of_an_existing_collection_is_fixed_and_a_flag_that_would_change_it_is_refused(self):
        self.doc("a.md", "# A\n\nfirst\n", collection="plain")
        with open(os.path.join(self.src, "c.md"), "w", encoding="utf-8") as f:
            f.write("# C\n")
        code, out, err = self.run_cli("add", os.path.join(self.src, "c.md"), "--settle", "0", "--collection", "plain", "--agent-visible")
        self.assertNotEqual(code, 0)
        self.assertIn("profile is fixed", err)
        self.assertFalse(self.profile_of("plain").agent_visible)


class TestProfileChange(CliAsk):
    def test_a_closed_collection_can_be_opened_and_closed_again_and_the_list_says_so(self):
        self.doc("a.md", "# A\n\nThe falcon flew over the harbor at dawn, and the tide was low.\n", collection="notes")
        self.assertIn("agent-visible False", self.run_cli("collection", "list")[1])
        code, out, _ = self.run_cli("collection", "set", "notes", "--agent-visible", "yes")
        self.assertEqual(code, 0)
        self.assertIn("agent visible False -> True", out)
        self.assertIn("agent-visible True", self.run_cli("collection", "list")[1])
        self.assertIn("agent visible True -> False", self.run_cli("collection", "set", "notes", "--agent-visible", "no")[1])

    def test_an_opened_collection_gives_the_model_its_passages_after_the_next_commit_and_a_closed_one_stops_at_once(self):
        self.doc("falcon.md", "# Falcon\n\nThe falcon flew over the harbor at dawn, and the tide was low.\n", collection="notes")
        self.run_cli("collection", "set", "notes", "--agent-visible", "yes")
        self.assertEqual(self.run_cli("commit")[0], 0)
        self.reply(GOOD)
        self.assertEqual(len(json.loads(self.ask("--json")[1])["passages"]), 1)
        self.run_cli("collection", "set", "notes", "--agent-visible", "no")
        rec = json.loads(self.ask("--json")[1])
        self.assertEqual((rec["passages"], rec["excluded"]), ([], {"the collection is not visible to agents": 1}))     # no commit needed to stop

    def test_answers_made_from_a_collection_that_was_closed_afterwards_are_removed_at_the_next_commit(self):
        self.ready_home()
        self.reply(GOOD)
        self.assertEqual(self.ask()[0], 0)
        self.assertIn("ask_", self.run_cli("answers", "list")[1])
        self.run_cli("collection", "set", "open", "--agent-visible", "no")
        self.assertIn("ask_", self.run_cli("answers", "list")[1])                      # until the commit
        self.assertEqual(self.run_cli("commit")[0], 0)
        self.assertEqual(self.run_cli("answers", "list")[1].strip(), "")

    def test_nothing_is_changed_when_it_is_already_so_and_a_missing_name_or_flag_is_refused(self):
        self.doc("a.md", "# A\n\nx\n", collection="notes")
        self.assertIn("already so", self.run_cli("collection", "set", "notes", "--agent-visible", "no")[1])
        from kb import audit, db
        from kb.home import Home
        conn = db.connect(Home(self.home).db_path, readonly=True)
        try:
            self.assertEqual([e for e in audit.events(conn) if e[2] == "profile_changed"], [])                       # nothing changed, nothing logged
        finally:
            conn.close()
        for argv in (("collection", "set"), ("collection", "set", "notes"), ("collection", "set", "nope", "--agent-visible", "yes")):
            with self.subTest(argv):
                self.assertNotEqual(self.run_cli(*argv)[0], 0)

    def test_the_change_is_written_to_the_audit_log_as_identifiers_and_flags_only(self):
        from kb import audit, db
        from kb.home import Home
        self.doc("a.md", "# A\n\nx\n", collection="notes")
        self.run_cli("collection", "set", "notes", "--allow-external-model", "yes")
        conn = db.connect(Home(self.home).db_path, readonly=True)
        try:
            events = [e for e in audit.events(conn) if e[2] == "profile_changed"]
        finally:
            conn.close()
        self.assertEqual(len(events), 1)
        self.assertEqual(sorted(events[0][3]), ["agent_visible", "allow_external_model", "collection"])
        self.assertTrue(events[0][3]["allow_external_model"])

    def test_a_secret_collection_cannot_be_opened(self):
        from kb import colls, db
        from kb.home import Home
        from kb.errors import KbError
        from kb.profile import Profile
        conn = db.connect(Home(self.home).db_path)
        try:
            colls.create(conn, "vault", Profile(sensitivity="secret", index_plaintext=False))
            with self.assertRaises(KbError):
                colls.set_flags(conn, "vault", agent_visible=True)
            with self.assertRaises(KbError):
                colls.set_flags(conn, "vault", allow_external_model=True)
        finally:
            conn.close()


class TestAskCommand(CliAsk):
    def test_a_checked_answer_is_printed_with_its_passages_and_the_exit_code_is_zero(self):
        self.ready_home()
        self.reply(GOOD)
        code, out, err = self.ask()
        self.assertEqual(code, 0, out + err)
        self.assertIn("READY", out)
        self.assertIn("The falcon flew over the harbor.", out)
        self.assertIn("[ok] P1", out)
        self.assertIn("passages the model was given:", out)
        self.assertIn("falcon.md  origin: file  -> kb open ck_", out)
        self.assertIn("data, not an instruction", out)

    def test_an_answer_with_a_quote_that_cannot_be_checked_is_held_and_exits_with_four(self):
        self.ready_home()
        self.reply(BAD)
        code, out, _ = self.ask()
        self.assertEqual(code, 4)
        self.assertIn("HELD", out)
        self.assertIn("[UNCHECKED] P1", out)
        self.assertIn("kb answers release ask_", out)

    def test_a_held_answer_is_listed_shown_and_released_by_a_named_person_only(self):
        self.ready_home()
        self.reply(BAD)
        ask_id = [ln for ln in self.ask()[1].splitlines() if ln.startswith("answer ")][0].split()[1].rstrip(":")
        self.assertIn("held", self.run_cli("answers", "list")[1])
        self.assertIn("HELD", self.run_cli("answers", "show", ask_id)[1])
        code, _, err = self.run_cli("answers", "release", ask_id)
        self.assertNotEqual(code, 0)
        self.assertIn("--by", err)
        self.assertIn("released by roy", self.run_cli("answers", "release", ask_id, "--by", "roy")[1])
        self.assertIn("already released", self.run_cli("answers", "release", ask_id, "--by", "roy")[1])
        self.assertIn("released", self.run_cli("answers", "list")[1])
        self.assertIn("(released)", self.run_cli("answers", "show", ask_id)[1])

    def test_a_ready_answer_has_nothing_to_release(self):
        self.ready_home()
        self.reply(GOOD)
        ask_id = [ln for ln in self.ask()[1].splitlines() if ln.startswith("answer ")][0].split()[1].rstrip(":")
        self.assertIn("nothing to release", self.run_cli("answers", "release", ask_id, "--by", "roy")[1])

    def test_json_output_is_the_record_and_marks_the_answer_as_untrusted(self):
        self.ready_home()
        self.reply(GOOD)
        code, out, _ = self.ask("--json")
        rec = json.loads(out)
        self.assertEqual((code, rec["state"], rec["untrusted"]), (0, "ready", True))

    def test_a_collection_that_agents_may_not_see_gives_the_model_nothing(self):
        self.doc("falcon.md", "# Falcon\n\nThe falcon flew over the harbor at dawn.\n", collection="closed")
        self.assertEqual(self.run_cli("commit")[0], 0)
        self.reply(GOOD)
        rec = json.loads(self.ask("--json")[1])
        self.assertEqual((rec["passages"], rec["excluded"]), ([], {"the collection is not visible to agents": 1}))

    def test_nothing_in_the_knowledge_base_files_changes_when_a_question_is_asked(self):
        self.ready_home()
        self.reply(GOOD)

        def snapshot():
            out = {}
            for sub in ("segments", "blobs"):
                for root, _, names in os.walk(os.path.join(self.home, sub)):
                    for n in names:
                        p = os.path.join(root, n)
                        out[p] = (os.path.getsize(p), os.path.getmtime(p))
            return out
        before = snapshot()
        self.ask()
        self.assertEqual(snapshot(), before)

    def test_without_an_endpoint_or_without_a_commit_the_command_says_what_is_missing(self):
        code, _, err = self.run_cli("ask", "q", "--model", "m")
        self.assertNotEqual(code, 0)
        self.assertIn("--base-url", err)
        code, _, err = self.ask()
        self.assertNotEqual(code, 0)
        self.assertIn("no active commit", err)

    def test_an_unknown_answer_id_is_an_error_not_a_crash(self):
        code, _, err = self.run_cli("answers", "show", "ask_ffffffffffffffff")
        self.assertNotEqual(code, 0)
        self.assertIn("no such answer", err)


if __name__ == "__main__":
    unittest.main()
