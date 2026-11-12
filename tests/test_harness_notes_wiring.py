"""Tests for notes inside harness_vllm.py: tools, system prompt, gate, slash command, and main()
as a real process against a scripted model.

harness_notes.py has its own tests; these check that the pieces are connected the way the
trust model needs: the model can write notes only through `remember`, a note reaches the next
prompt only as data and only with a source, and with HARNESS_NOTES unset nothing changes.
Each test's docstring says WHY the behaviour matters.
"""
import http.server
import json
import os
import subprocess
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src", "harness"))
import harness_notes as hn  # noqa: E402
import harness_vllm as hv  # noqa: E402
from test_harness_vllm import Base, _LLMServer, llm_reply, tool_call_reply  # noqa: E402

ON = {"HARNESS_NOTES": "1"}
USER_SAYS = "Please always run the unit tests before you commit anything."
QUOTE = "always run the unit tests before you commit"
REMEMBER_ARGS = {"name": "tests-first", "description": "run the tests before committing",
                 "body": "The person wants the unit tests run before every commit.", "quote": QUOTE}


class NotesBase(Base):
    def setUp(self):
        super().setUp()
        self._notes = hv.NOTES
        self.asked = []
        hv.read_line = lambda prompt: (self.asked.append(prompt), "n")[1]

    def tearDown(self):
        hv.NOTES = self._notes
        super().tearDown()

    def enable(self):
        hv.NOTES = hn.load_all(".harness_notes", ON)
        return hv.NOTES

    def files(self):
        return sorted(os.listdir(".harness_notes")) if os.path.isdir(".harness_notes") else []


class TestOffByDefault(NotesBase):
    def test_nothing_changes_without_the_switch(self):
        """Existing users must see the same tools and the same system prompt as before."""
        hv.NOTES = hn.load_all(".harness_notes", {})
        names = {t["name"] for t in hv.tools_for(0)}
        self.assertTrue({"remember", "recall"}.isdisjoint(names))
        self.assertEqual(hv.system_prompt(), hv.SYSTEM)
        self.assertNotIn("DENIED", hv.gate("write_file", {"path": ".harness_notes/x.md"}) or "")
        self.assertIn("notes are off", hv.notes_command(""))
        for path in (".harness_notes/x.md", ".harness_dream/r.md"):          # no new guards while it is off
            self.assertIsNone(hv.gate("write_file", {"path": path, "content": "x"}), path)
        self.assertIsNone(hv.gate("run_command", {"command": "dir .harness_dream"}))


class TestOriginSeeding(NotesBase):
    def test_only_the_persons_own_words_become_quotable_as_user_words(self):
        """A resumed session is rebuilt from saved messages. Reminders, compaction summaries,
        the model's own replies and tool output must not turn into 'the person said it'."""
        self.enable()
        summary = ("<system-reminder>\n" + hv.SUMMARY_MARK + " Summary of everything before this point:\n"
                   "SUMMARY-MARKER-CHARLIE the model retold this\n\n"
                   + hv.encode_verbatim(["VERBATIM-MARKER-DELTA the person typed this"], 0)
                   + "\n</system-reminder>")
        hv.seed_origin([
            {"role": "user", "content": summary},
            {"role": "user", "content": "<system-reminder>\nREMINDER-MARKER-ALPHA harness event\n"
                                        "</system-reminder>\nHUMAN-MARKER-BRAVO the person typed this too"},
            {"role": "assistant", "content": "ASSISTANT-MARKER-ECHO the model said this"},
            {"role": "tool", "tool_call_id": "c1", "content": "TOOL-MARKER-FOXTROT a tool returned this"},
        ])
        origin = hv.SEEN.origin_of
        self.assertEqual(origin("HUMAN-MARKER-BRAVO the person typed"), "user")
        self.assertEqual(origin("VERBATIM-MARKER-DELTA the person typed"), "user")
        self.assertEqual(origin("REMINDER-MARKER-ALPHA harness event"), "tool")
        self.assertEqual(origin("SUMMARY-MARKER-CHARLIE the model retold"), "tool")
        self.assertEqual(origin("TOOL-MARKER-FOXTROT a tool returned"), "tool")
        self.assertIsNone(origin("ASSISTANT-MARKER-ECHO the model said"))


class TestTools(NotesBase):
    def test_tools_exist_only_for_the_main_agent(self):
        """A subagent briefed by injected text must not be able to leave notes behind."""
        self.enable()
        self.assertTrue({"remember", "recall"} <= {t["name"] for t in hv.tools_for(0)})
        for depth in (1, 2):
            self.assertTrue({"remember", "recall"}.isdisjoint({t["name"] for t in hv.tools_for(depth)}), depth)
        for name in ("remember", "recall"):
            out = hv.execute_tool(name, dict(REMEMBER_ARGS), depth=1)
            self.assertIn("not available to subagents", out)
        self.assertEqual(self.files(), [])

    def test_remember_works_from_what_the_person_typed(self):
        """The ordinary path: the person says it, the model files it with their words as the quote."""
        self.enable()
        hv.ORIGIN.user_text(USER_SAYS)
        out = hv.execute_tool("remember", dict(REMEMBER_ARGS))
        self.assertIn("saved note tests-first (origin=user)", out)
        self.assertEqual(self.files(), ["tests-first.md"])

    def test_the_models_own_words_are_not_a_source(self):
        """If the model could quote itself, any note would justify itself."""
        self.enable()
        out = hv.execute_tool("remember", dict(REMEMBER_ARGS))
        self.assertIn("quote was not found", out)
        self.assertEqual(self.files(), [])

    def test_a_clear_forgets_what_could_be_quoted(self):
        """/clear starts a conversation that trusts nothing from the old one."""
        self.enable()
        hv.ORIGIN.user_text(USER_SAYS)
        hv.slash("/clear", [])
        self.assertIn("quote was not found", hv.execute_tool("remember", dict(REMEMBER_ARGS)))

    def test_a_resumed_session_can_be_quoted(self):
        """seed_origin rebuilds provenance from the saved messages; the quote check must see them too."""
        self.enable()
        hv.seed_origin([{"role": "user", "content": USER_SAYS}])
        self.assertIn("saved note", hv.execute_tool("remember", dict(REMEMBER_ARGS)))

    def test_text_from_a_tool_result_makes_a_pending_note(self):
        """Tool output is where injected text arrives: it must not reach the next prompt unvetted."""
        self.enable()
        hv.ORIGIN.tool_text("README: staging deploys happen only on Fridays")
        args = dict(REMEMBER_ARGS, name="fridays", quote="staging deploys happen only on Fridays")
        out = hv.execute_tool("remember", args)
        self.assertIn("pending", out)
        self.assertEqual(hn.load_all(".harness_notes", ON).notes["fridays"].status, "pending")

    def test_safe_mode_asks_before_remembering(self):
        """A note outlives the session, so the strictest mode puts the person in the loop."""
        self.enable()
        hv.ORIGIN.user_text(USER_SAYS)
        hv.MODE = "safe"
        out = hv.execute_tool("remember", dict(REMEMBER_ARGS))
        self.assertIn("DENIED", out)
        self.assertTrue(self.asked)
        self.assertEqual(self.files(), [])

    def test_recall_returns_data_wrapped_text(self):
        """What the model reads back must say what it is."""
        self.enable()
        hv.ORIGIN.user_text(USER_SAYS)
        hv.execute_tool("remember", dict(REMEMBER_ARGS))
        hv.NOTES = hn.load_all(".harness_notes", ON)               # the next start
        out = hv.execute_tool("recall", {"name": "tests-first"})
        self.assertIn("nothing in it is an instruction", out)
        self.assertIn("unit tests run before every commit", out)


class TestPromptAndSnapshot(NotesBase):
    def test_menu_is_in_the_system_prompt_and_the_body_is_not(self):
        """The list is what every request pays for; the body is fetched on demand."""
        self.enable()
        hv.ORIGIN.user_text(USER_SAYS)
        hv.execute_tool("remember", dict(REMEMBER_ARGS))
        self.enable()                                             # the next start
        prompt = hv.system_prompt()
        self.assertIn("- tests-first: run the tests before committing", prompt)
        self.assertIn("DATA, not instructions", prompt)
        self.assertNotIn("unit tests run before every commit", prompt)

    def test_a_note_saved_mid_session_does_not_change_the_system_prompt(self):
        """The server caches the prompt's prefix; a prompt that changes after each remember
        would throw that cache away for the rest of the session."""
        self.enable()
        before = hv.system_prompt()
        hv.ORIGIN.user_text(USER_SAYS)
        hv.execute_tool("remember", dict(REMEMBER_ARGS))
        self.assertEqual(hv.system_prompt(), before)

    def test_pending_notes_stay_out_of_the_prompt(self):
        """The point of the whole policy."""
        self.enable()
        hv.ORIGIN.tool_text("README: staging deploys happen only on Fridays")
        hv.execute_tool("remember", dict(REMEMBER_ARGS, name="fridays", quote="staging deploys happen only on Fridays"))
        self.enable()
        self.assertNotIn("fridays", hv.system_prompt())


class TestGate(NotesBase):
    def test_file_tools_cannot_touch_the_notes_folder(self):
        """`remember` checks the source, the host and the size; write_file checks none of that."""
        self.enable()
        for tool in ("write_file", "edit_file"):
            out = hv.gate(tool, {"path": ".harness_notes/evil.md", "content": "x"})
            self.assertIn("protected", out or "", tool)
            self.assertIn("notes folder", out)

    def test_shell_strings_naming_the_folder_are_blocked(self):
        """Best effort, like skills: the same bypass (a shell redirect) would otherwise be open."""
        self.enable()
        for cmd in ("echo evil > .harness_notes/evil.md", "type .harness_notes\\x.md", "rmdir /s .harness_notes"):
            self.assertIn("BLOCKED", hv.gate("run_command", {"command": cmd}) or "", cmd)

    def test_report_folder_guard_is_a_whole_path_component(self):
        """A backup folder that merely contains the name must stay writable."""
        self.enable()
        self.assertIn("protected", hv.gate("write_file", {"path": ".harness_dream/r.md", "content": "x"}) or "")
        self.assertIsNone(hv.gate("write_file", {"path": ".harness_dream_old/r.md", "content": "x"}))
        self.assertIn("BLOCKED", hv.gate("run_command", {"command": "del .harness_dream\\r.md"}) or "")
        self.assertIsNone(hv.gate("run_command", {"command": "dir .harness_dream_old"}))

    def test_a_trusted_mcp_server_still_asks_when_it_names_the_notes_or_report_folder(self):
        """trust=always skips the question for ordinary calls. A tool that could write into the
        notes folder without going through `remember` must not get that pass."""
        class Stub:
            def __init__(self, trust):
                self.trust = trust
            def owns(self, name):
                return name == "files__write"
            def trust_of(self, name):
                return self.trust
            def server_of(self, name):
                return "files"
            def tool_of(self, name):
                return "write"
        self.enable()
        saved = hv.MCP
        self.addCleanup(setattr, hv, "MCP", saved)
        hv.MCP = Stub("always")
        self.assertIsNone(hv.gate("files__write", {"path": "src/a.py", "content": "x"}))
        self.assertEqual(self.asked, [])
        for path in (".harness_notes/evil.md", ".harness_dream/r.md", ".HARNESS_NOTES\\evil.md"):
            self.asked.clear()
            out = hv.gate("files__write", {"path": path, "content": "x"})
            self.assertIn("DENIED", out or "", path)
            self.assertEqual(len(self.asked), 1, path)
        self.asked.clear()
        self.assertIsNone(hv.gate("files__write", {"path": ".harness_notes_old/a.md"}))       # not this folder
        hv.MCP = Stub("ask")
        self.asked.clear()
        self.assertIn("DENIED", hv.gate("files__write", {"path": "src/a.py"}) or "")           # the ordinary rule

    def test_reading_the_folder_is_not_blocked(self):
        """The person's own notes are theirs to look at; only writes are guarded."""
        self.enable()
        self.assertIsNone(hv.gate("read_file", {"path": ".harness_notes/x.md"}))


class TestSlash(NotesBase):
    def test_the_person_can_list_show_promote_and_forget(self):
        """The person's side of the policy: a pending note reaches the prompt only this way."""
        self.enable()
        hv.ORIGIN.tool_text("README: staging deploys happen only on Fridays")
        hv.execute_tool("remember", dict(REMEMBER_ARGS, name="fridays", quote="staging deploys happen only on Fridays"))
        self.assertIn("fridays [tool/pending]", hv.notes_command(""))
        self.assertIn("quote:", hv.notes_command("show fridays"))
        self.assertIn("promoted", hv.notes_command("promote fridays"))
        self.enable()
        self.assertIn("- fridays:", hv.system_prompt())
        self.assertEqual(hv.notes_command("forget fridays"), "forgot fridays")
        self.assertEqual(self.files(), [])
        self.assertIn("usage", hv.notes_command("nonsense"))

    def test_the_slash_command_is_listed_in_help(self):
        """A command nobody can find does not exist."""
        self.assertIn("/notes", hv.HELP)


class TestDreamCommand(NotesBase):
    """/dream through the harness: the model call has no tools, the report is the only write,
    and apply touches the notes folder only."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.messages = [{"role": "user", "content": USER_SAYS}]
        hv.MEMORY = ""
        hv._DREAM[0] = None

    def tearDown(self):
        hv._DREAM[0] = None
        super().tearDown()

    def model_says(self, *proposals):
        def fake(messages, tools, system, **kw):
            self.calls.append((messages, tools, system, kw))
            return {"choices": [{"message": {"content": json.dumps({"proposals": list(proposals)})}}]}
        hv.call_llm = fake

    @staticmethod
    def proposal(name="tests-first"):
        return {"op": "add", "name": name, "description": "run the tests before committing",
                "body": "Run the unit tests before every commit.", "why_not_derivable": "a preference",
                "evidence": [{"quote": QUOTE, "where": "S1#0"}]}

    def test_dream_is_off_without_notes(self):
        """It works on the notes folder, so without notes there is nothing for it to do."""
        hv.NOTES = hn.load_all(".harness_notes", {})
        self.assertIn("notes are off", hv.dream_command("", self.messages))

    def test_the_model_gets_no_tools_and_only_a_report_is_written(self):
        """Text inside a session must not be able to reach a tool; the person reads first."""
        self.enable()
        self.model_says(self.proposal())
        out = hv.dream_command("", self.messages)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.calls[0][1])                       # tools=None
        self.assertIn("1 proposal(s) kept", out)
        self.assertIn("nothing has been applied", out)
        self.assertEqual(self.files(), [])
        reports = os.listdir(".harness_dream")
        self.assertEqual(len(reports), 1)

    def test_apply_writes_the_note_and_only_the_named_ones(self):
        """The person chooses; apply works from the proposals held in memory."""
        self.enable()
        self.model_says(self.proposal("a1"), self.proposal("a2"))
        hv.dream_command("", self.messages)
        self.assertIn("saved note a2", hv.dream_command("apply p2", self.messages))
        self.assertEqual(self.files(), ["a2.md"])

    def test_apply_needs_a_dream_first_and_names(self):
        """No proposals, nothing to apply; and `apply` alone does not mean 'everything'."""
        self.enable()
        self.assertIn("run /dream first", hv.dream_command("apply p1", self.messages))
        self.model_says(self.proposal())
        hv.dream_command("", self.messages)
        self.assertIn("usage", hv.dream_command("apply", self.messages))
        self.assertEqual(self.files(), [])

    def test_a_failed_dream_leaves_nothing_to_apply(self):
        """A stale result from an earlier run must not be applied after a failure."""
        self.enable()
        self.model_says(self.proposal())
        hv.dream_command("", self.messages)
        hv.call_llm = lambda *a, **k: {"choices": [{"message": {"content": "no json"}}]}
        self.assertIn("dream failed", hv.dream_command("", self.messages))
        self.assertIn("run /dream first", hv.dream_command("apply p1", self.messages))

    def test_other_session_files_can_be_named(self):
        """The raw layer is the session files; a quote from S2 must be verifiable."""
        self.enable()
        with open("old.json", "w", encoding="utf-8") as f:
            json.dump([{"role": "user", "content": "We decided long ago: never force-push to main."}], f)
        p = self.proposal("no-force-push")
        p["evidence"] = [{"quote": "never force-push to main", "where": "S2#0"}]
        self.model_says(p)
        self.assertIn("1 proposal(s) kept", hv.dream_command("old.json", self.messages))
        self.assertIn("cannot read", hv.dream_command("missing.json", self.messages))
        with open("bad.json", "w", encoding="utf-8") as f:
            f.write('{"not": "a list"}')
        self.assertIn("not a session file", hv.dream_command("bad.json", self.messages))

    def test_the_model_cannot_write_the_dream_folder(self):
        """The report is for the person; a model that could edit it could shape what they approve."""
        self.enable()
        self.assertIn("protected", hv.gate("write_file", {"path": ".harness_dream/dream-x.md", "content": "x"}) or "")
        self.assertIn("BLOCKED", hv.gate("run_command", {"command": "del .harness_dream\\dream-x.md"}) or "")


# --------------------------------------------------------------- end to end
class TestEndToEnd(NotesBase):
    """main() as a real process against a scripted model, twice: save, then start again."""

    def setUp(self):
        super().setUp()
        _LLMServer.script, _LLMServer.bodies = [], []
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LLMServer)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        super().tearDown()

    def run_harness(self, stdin_text, session="session.json", extra=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("VLLM_", "HARNESS_"))}
        env.update(HARNESS_BASE_URL=f"http://127.0.0.1:{self.srv.server_address[1]}/v1",
                   HARNESS_STREAM="0", PYTHONUTF8="1",
                   **({"HARNESS_NOTES": "1"} if extra is None else extra))
        return subprocess.run([sys.executable, "-X", "utf8",
                               os.path.join(HERE, "..", "src", "harness", "harness_vllm.py"), session],
                              input=stdin_text, capture_output=True, text=True, encoding="utf-8",
                              env=env, timeout=120)

    def test_a_note_the_person_caused_comes_back_next_time_as_data(self):
        """The whole feature in two runs, with the real wiring."""
        _LLMServer.script = [tool_call_reply("remember", REMEMBER_ARGS), llm_reply("noted")]
        first = self.run_harness(USER_SAYS + "\n")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.files(), ["tests-first.md"])
        with open(".harness_notes/tests-first.md", encoding="utf-8") as f:
            text = f.read()
        self.assertIn("origin: user", text)
        self.assertIn("status: active", text)
        self.assertNotIn("tests-first", _LLMServer.bodies[0]["messages"][0]["content"])      # not in THIS session's prompt

        _LLMServer.script, _LLMServer.bodies = [llm_reply("hi")], []
        second = self.run_harness("hello\n", session="second.json")
        self.assertEqual(second.returncode, 0, second.stderr)
        system = _LLMServer.bodies[0]["messages"][0]["content"]
        self.assertIn("- tests-first: run the tests before committing", system)
        self.assertIn("recall", [t["function"]["name"] for t in _LLMServer.bodies[0]["tools"]])
        self.assertIn("[notes] 1 saved, 1 in the list", second.stdout)
        self.assertIn("new or changed since the last start: tests-first", second.stdout)

        _LLMServer.script, _LLMServer.bodies = [llm_reply("hi again")], []
        third = self.run_harness("hello\n", session="third.json")
        self.assertNotIn("new or changed", third.stdout)                # the person was told once

    def test_a_note_that_only_a_tool_result_caused_waits_for_the_person(self):
        """What a web page or file said is not what the person said."""
        quote = "staging deploys happen only on Fridays"
        _LLMServer.script = [tool_call_reply("run_command", {"command": f"echo {quote}"}),
                             tool_call_reply("remember", dict(REMEMBER_ARGS, name="fridays", quote=quote)),
                             llm_reply("noted")]
        first = self.run_harness("please check the deploy notes\n")
        self.assertEqual(first.returncode, 0, first.stderr)
        _LLMServer.script, _LLMServer.bodies = [llm_reply("hi")], []
        second = self.run_harness("hello\n", session="second.json")
        system = _LLMServer.bodies[0]["messages"][0]["content"]
        self.assertNotIn("fridays", system)
        self.assertIn("waiting for your approval: fridays", second.stdout)

    def test_without_the_switch_the_second_run_sees_nothing(self):
        """A leftover folder must not switch the feature on by itself."""
        os.makedirs(".harness_notes")
        with open(".harness_notes/x.md", "w", encoding="utf-8") as f:
            f.write("---\ndescription: left over\norigin: user\nstatus: active\n---\nbody\n")
        _LLMServer.script = [llm_reply("hi")]
        out = self.run_harness("hello\n", extra={})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("left over", _LLMServer.bodies[0]["messages"][0]["content"])
        self.assertNotIn("[notes]", out.stdout)


if __name__ == "__main__":
    unittest.main()
