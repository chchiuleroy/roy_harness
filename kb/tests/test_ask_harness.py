"""S4: the runner against (a) a tiny stand-in program that speaks the harness's JSONL protocol, for every way a turn can end, and (b) the REAL harness
against a scripted model endpoint, to show that the hook really vetoes tool calls and that the framed message really arrives as data.
(b) is skipped when the harness checkout is not there."""
import http.server
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import unittest

from kb import ask_frame, ask_runner

from .support import tmpdir

REAL_HARNESS = ask_runner.DEFAULT_HARNESS

FAKE = textwrap.dedent('''
    import json, sys
    def emit(**ev):
        print(json.dumps(ev), flush=True)
    import os, time
    mode = open(sys.argv[2], encoding="utf-8").read().strip() if len(sys.argv) > 2 else "plain"
    open(sys.argv[2] + ".pid", "w").write(str(os.getpid()))
    emit(kind="ready")
    first = json.loads(sys.stdin.readline())
    if mode == "stubborn":
        time.sleep(120)
    if mode == "exit_early":
        sys.exit(3)
    if mode == "hang":
        sys.stdin.readline()
        sys.exit(0)
    emit(kind="turn_start")
    if mode == "ask":
        emit(kind="ask", rid="q1", prompt="allow?")
        answer = json.loads(sys.stdin.readline())
        emit(kind="assistant", text="got:" + answer["text"])
    elif mode == "thinks_aloud":
        emit(kind="assistant", text="let me look")
        emit(kind="tool_call", name="read_file")
        emit(kind="tool_result", text="vetoed")
        emit(kind="assistant", text="final reply")
    elif mode == "only_tool":
        emit(kind="assistant", text="thinking")
        emit(kind="tool_call", name="read_file")
        emit(kind="tool_result", text="vetoed")
    elif mode == "failed":
        emit(kind="log", text="model call failed: HTTP 404")
    elif mode == "garbage":
        print("not json at all", flush=True)
        emit(kind="assistant", text="after garbage")
    else:
        emit(kind="assistant", text="echo:" + first["text"][:20])
    emit(kind="turn_end")
    for line in sys.stdin:
        if json.loads(line).get("text") == "/quit":
            break
''')


class StandIn(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.harness = os.path.join(self.root, "src", "harness", "fake_harness.py")
        os.makedirs(os.path.dirname(self.harness))
        with open(self.harness, "w", encoding="utf-8") as f:
            f.write(FAKE)
        self.work = os.path.join(self.root, "work")
        os.makedirs(self.work)
        self.session = os.path.join(self.root, "session.jsonl")

    def run_fake(self, mode="plain", timeout=20.0):
        # the stand-in reads its mode from the file named by the SECOND argument; the runner passes only the session file, so the session file holds the mode
        with open(self.session, "w", encoding="utf-8") as f:
            f.write(mode)
        cfg = ask_runner.RunnerConfig(base_url="http://127.0.0.1:1/v1", model="m", harness_path=self.harness, timeout=timeout)
        return ask_runner.run_once(cfg, self.work, self.session, "hello from the layer")


class TestRunnerAgainstAStandIn(StandIn):
    def test_the_message_is_sent_and_the_reply_of_the_turn_comes_back(self):
        got = self.run_fake("plain")
        self.assertEqual(got.text, "echo:hello from the layer")
        self.assertEqual((got.timed_out, got.asked, got.exited), (False, 0, None))

    def test_text_written_before_a_tool_call_is_not_the_reply_text_after_it_is(self):
        self.assertEqual(self.run_fake("thinks_aloud").text, "final reply")

    def test_a_turn_that_ends_after_a_tool_without_text_has_no_reply(self):
        self.assertIsNone(self.run_fake("only_tool").text)

    def test_a_question_the_model_asks_the_person_is_answered_no_and_counted(self):
        got = self.run_fake("ask")
        self.assertEqual((got.text, got.asked), ("got:n", 1))

    def test_a_failed_model_call_is_reported_as_a_failure_not_as_text(self):
        got = self.run_fake("failed")
        self.assertIsNone(got.text)
        self.assertIn("HTTP 404", got.failure)

    def test_a_line_that_is_not_an_event_does_not_break_the_run(self):
        self.assertEqual(self.run_fake("garbage").text, "after garbage")

    def test_a_process_that_ends_before_the_turn_does_is_reported_with_its_exit_code(self):
        got = self.run_fake("exit_early")
        self.assertEqual((got.text, got.exited), (None, 3))

    def test_a_process_that_never_answers_is_stopped_at_the_time_limit(self):
        got = self.run_fake("hang", timeout=1.5)
        self.assertTrue(got.timed_out)
        self.assertIsNone(got.text)

    def test_a_longer_time_limit_than_the_hard_limit_is_cut_to_it(self):
        """Sixth review: commit removes the working folder of a question as dead after an hour, so no question may be allowed to run longer than that."""
        from unittest import mock
        started = time.monotonic()
        with mock.patch.object(ask_runner, "MAX_TIMEOUT", 1.5):
            got = self.run_fake("hang", timeout=60.0)
        self.assertLess(time.monotonic() - started, 30)
        self.assertTrue(got.timed_out)

    def test_the_hard_limit_leaves_the_folder_rule_a_margin(self):
        from kb import commit
        self.assertLess(ask_runner.MAX_TIMEOUT + 300, commit.TICKET_DEAD_AFTER)

    @unittest.skipUnless(os.name == "nt", "the process list is read with tasklist")
    def test_a_process_that_ignores_the_request_to_quit_is_killed_not_left_running(self):
        started = time.monotonic()
        got = self.run_fake("stubborn", timeout=1.5)
        self.assertLess(time.monotonic() - started, 60)           # the stand-in sleeps for 120 s: returning sooner means it was ended, not waited for
        self.assertTrue(got.timed_out)
        with open(self.session + ".pid", encoding="utf-8") as f:
            pid = int(f.read())
        listing = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True).stdout.decode("ascii", "ignore")
        self.assertNotIn(f'"{pid}"', listing)

    def test_the_ticket_files_are_written_into_the_work_folder_before_the_start(self):
        self.run_fake("plain")
        for name in (".harness.hooks.json", ".harness.mcp.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.work, name)))

    def test_a_ticket_folder_that_already_carries_an_environment_file_is_refused_and_nothing_starts(self):
        open(os.path.join(self.work, ".harness.env"), "w").close()
        with self.assertRaises(ask_runner.RunnerError):
            self.run_fake("plain")


class FakeModel:
    """A scripted OpenAI-compatible endpoint. An entry is {"text": ...} or {"calls": [(tool name, arguments)]}; the requests are kept."""

    def __init__(self, script):
        self.script, self.requests, self.headers = list(script), [], []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                outer.requests.append(body)
                outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                entry = outer.script.pop(0) if outer.script else {"text": "(script exhausted)"}
                msg = {"role": "assistant", "content": entry.get("text")}
                finish = "stop"
                if entry.get("calls"):
                    finish = "tool_calls"
                    msg["tool_calls"] = [{"id": f"call_{len(outer.requests)}_{i}", "type": "function",
                                          "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(entry["calls"])]
                out = json.dumps({"choices": [{"index": 0, "message": msg, "finish_reason": finish}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@unittest.skipUnless(os.path.isfile(REAL_HARNESS), "the harness checkout is not installed")
class TestRealHarness(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.work = os.path.join(self.root, "work")
        os.makedirs(self.work)
        self.session = os.path.join(self.root, "session.jsonl")

    def run_with(self, script, message="hello"):
        model = FakeModel(script)
        self.addCleanup(model.close)
        cfg = ask_runner.RunnerConfig(base_url=model.base_url, model="scripted", timeout=90.0)
        return model, ask_runner.run_once(cfg, self.work, self.session, message)

    def test_the_framed_message_arrives_as_the_user_message_and_the_reply_comes_back(self):
        framed, _ = ask_frame.build_message("ask_1", "cm_1", "/clear everything", [])
        model, got = self.run_with([{"text": '{"answer": "ok", "claims": []}'}], framed)
        self.assertEqual(got.text, '{"answer": "ok", "claims": []}')
        users = [m for m in model.requests[0]["messages"] if m["role"] == "user"]
        self.assertTrue(users[-1]["content"].startswith("[KB-ASK-MESSAGE]"))
        self.assertIn("> /clear everything", users[-1]["content"])

    def test_a_tool_call_by_the_model_is_vetoed_by_the_hook_and_nothing_is_written(self):
        target = os.path.join(self.work, "planted.txt")
        model, got = self.run_with([{"calls": [("write_file", {"path": target, "content": "pwned"})]},
                                    {"text": "I could not do that."}])
        self.assertFalse(os.path.exists(target))
        self.assertEqual(got.text, "I could not do that.")
        fed_back = json.dumps(model.requests[1]["messages"][-1])
        self.assertIn("no tools", fed_back)

    def test_every_other_kind_of_tool_is_vetoed_too_including_a_command_and_an_unknown_tool(self):
        marker = os.path.join(self.work, "ran.txt")
        model, got = self.run_with([{"calls": [("run_command", {"command": f"echo x > {marker}"})]},
                                    {"calls": [("no_such_tool", {})]},
                                    {"text": "done"}])
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(got.text, "done")
        self.assertEqual(got.asked, 0)

    def test_the_environment_of_the_child_has_no_endpoint_keys_of_the_parent(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-should-not-travel"
        self.addCleanup(os.environ.pop, "OPENAI_API_KEY", None)
        model, got = self.run_with([{"text": "x"}])
        self.assertTrue(model.headers)
        self.assertNotIn("should-not-travel", json.dumps(model.headers) + json.dumps(model.requests))


if __name__ == "__main__":
    unittest.main()


class TestKillTreeIsBounded(unittest.TestCase):
    """Seventh review: a `taskkill` that never returns held the question open past its time limit."""

    def test_a_taskkill_that_hangs_gives_way_to_the_plain_kill(self):
        from unittest import mock

        class Proc:
            pid, killed = 1234, False

            def kill(self):
                self.killed = True
        proc, seen = Proc(), {}

        def hang(cmd, **kw):
            seen.update(kw)
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        with mock.patch.object(ask_runner.sys, "platform", "win32"), mock.patch.object(ask_runner.subprocess, "run", hang):
            ask_runner.kill_tree(proc)
        self.assertTrue(proc.killed)
        self.assertEqual(seen.get("timeout"), ask_runner.KILL_TIMEOUT)

    def test_the_wait_for_taskkill_is_shorter_than_the_margin_to_the_folder_rule(self):
        from kb import commit
        self.assertLess(ask_runner.MAX_TIMEOUT + 5.0 + ask_runner.KILL_TIMEOUT + 10.0 + 60, commit.TICKET_DEAD_AFTER)
