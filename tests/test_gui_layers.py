"""Tests for the GUI's settings page and the optional layers (src/gui_layers/, plus the generic hook in harness_gui.py).

Two promises are checked here:
  * the switch is real: a layer that is off is refused by the SERVER, not only hidden by the page;
  * the harness folder still knows nothing about the layers (the rule superagent's and kb's own tests rely on).
Each test's docstring says WHY the behaviour matters.
"""
import ast
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path[:0] = [SRC, os.path.join(SRC, "harness")]
import harness_gui as gui  # noqa: E402
from gui_layers.kb_panel import KbLayer  # noqa: E402
from gui_layers.superagent_panel import SuperagentLayer  # noqa: E402

TOKEN = "layers-token"


class Recorder:
    """A fake command runner: remembers what it was asked to run, answers from a script."""
    def __init__(self, reply=(0, "", "")):
        self.calls, self.reply = [], reply

    def __call__(self, args, pythonpath, cwd, timeout):
        self.calls.append(list(args))
        return self.reply(args) if callable(self.reply) else self.reply


class FakeLayer:
    key, title, description, default_enabled = "fake", "Fake", "a layer for tests", True
    fields = [{"key": "home", "label": "home", "default": "/default"}]

    def __init__(self):
        self.acts, self.panels = [], 0

    def panel(self, values):
        self.panels += 1
        return {"title": "Fake", "status": "ok", "blocks": [{"type": "text", "text": values["home"]}]}

    def act(self, values, action, args):
        self.acts.append((action, args, dict(values)))
        if action == "bad":
            raise ValueError("no good")
        if action == "boom":
            raise RuntimeError("secret detail")
        return {"ok": True, "message": "done"}


class ServerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self._tmp.name, "home")
        self._patches = [mock.patch.object(gui, "HOME", self.home),
                         mock.patch.object(gui, "SESSIONS", os.path.join(self.home, "sessions")),
                         mock.patch.object(gui, "REGISTRY", gui.Registry()),
                         mock.patch.dict(gui.EXTENSIONS, {}, clear=True)]
        for p in self._patches:
            p.start()
        self.fake = FakeLayer()
        gui.register_extension(self.fake)
        self.srv = gui.make_server(0, TOKEN, self._tmp.name)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        h = {"Host": f"127.0.0.1:{self.port}", "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, json.loads(data)

    def layer(self, key="fake"):
        return next(l for l in self.call("GET", "/api/settings")[1]["layers"] if l["key"] == key)


class TestSwitches(ServerBase):
    def test_a_layer_is_on_or_off_by_its_own_default_until_the_person_decides(self):
        """superagent starts on and kb starts off: that is each layer's own default, kept by the layer."""
        other = FakeLayer()
        other.key, other.default_enabled = "other", False
        gui.register_extension(other)
        self.assertEqual((self.layer("fake")["enabled"], self.layer("other")["enabled"]), (True, False))
        self.assertFalse(os.path.exists(gui.settings_path()))        # looking does not write anything

    def test_the_real_layers_start_the_way_the_person_asked(self):
        self.assertIs(SuperagentLayer.default_enabled, True)
        self.assertIs(KbLayer.default_enabled, False)

    def test_a_switch_is_remembered_across_a_restart(self):
        st, v = self.call("POST", "/api/settings", {"layer": "fake", "enabled": False})
        self.assertEqual((st, v["enabled"]), (200, False))
        with open(gui.settings_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["layers"]["fake"]["enabled"], False)
        self.assertFalse(self.layer()["enabled"])                    # read back from the file
        self.call("POST", "/api/settings", {"layer": "fake", "enabled": True})
        self.assertTrue(self.layer()["enabled"])

    def test_switching_off_closes_the_door_on_the_server(self):
        """Hiding a button is not a switch: a request that arrives anyway (an old tab, a script)
        must be refused, and the layer must not be asked."""
        self.call("POST", "/api/settings", {"layer": "fake", "enabled": False})
        self.assertEqual(self.call("GET", "/api/layers/fake/panel")[0], 409)
        self.assertEqual(self.call("POST", "/api/layers/fake/act", {"action": "x", "args": {}})[0], 409)
        self.assertEqual((self.fake.panels, self.fake.acts), (0, []))
        self.call("POST", "/api/settings", {"layer": "fake", "enabled": True})
        self.assertEqual(self.call("GET", "/api/layers/fake/panel")[0], 200)

    def test_one_layer_switch_does_not_move_another(self):
        other = FakeLayer()
        other.key = "other"
        gui.register_extension(other)
        self.call("POST", "/api/settings", {"layer": "fake", "enabled": False})
        self.assertTrue(self.layer("other")["enabled"])

    def test_settings_are_text_only_and_only_the_layers_own(self):
        for body in ({"layer": "nope", "enabled": True}, {"layer": ["fake"]}, {"layer": "fake", "enabled": "yes"},
                     {"layer": "fake", "fields": {"unknown": "x"}}, {"layer": "fake", "fields": {"home": 5}},
                     {"layer": "fake", "fields": {"home": "x" * (gui.FIELD_MAX + 1)}},
                     {"layer": "fake", "fields": {"home": "a\x00b"}}, {"layer": "fake", "fields": ["home"]}):
            self.assertEqual(self.call("POST", "/api/settings", body)[0], 400, body)
        self.assertFalse(os.path.exists(gui.settings_path()))        # none of them wrote a thing

    def test_a_field_is_kept_and_a_later_switch_does_not_lose_it(self):
        self.call("POST", "/api/settings", {"layer": "fake", "fields": {"home": "/mine"}})
        self.call("POST", "/api/settings", {"layer": "fake", "enabled": False})
        self.assertEqual(self.layer()["fields"][0]["value"], "/mine")
        self.call("POST", "/api/settings", {"layer": "fake", "enabled": True})
        self.assertEqual(self.call("GET", "/api/layers/fake/panel")[1]["blocks"][0]["text"], "/mine")

    def test_a_broken_settings_file_falls_back_to_the_defaults(self):
        os.makedirs(self.home)
        with open(gui.settings_path(), "w", encoding="utf-8") as f:
            f.write('{"layers": {"fake": {"enabled": "maybe", "fields": {"home": 3}}}')
        view = self.layer()
        self.assertEqual((view["enabled"], view["fields"][0]["value"]), (True, "/default"))

    def test_without_any_layer_the_page_is_told_how_to_get_them(self):
        gui.EXTENSIONS.clear()
        st, j = self.call("GET", "/api/settings")
        self.assertEqual((st, j["layers"]), (200, []))
        self.assertIn("gui_layers", j["hint"])


class TestLayerRequests(ServerBase):
    def test_every_layer_endpoint_has_the_guards_of_the_rest(self):
        for method, path, body in (("GET", "/api/settings", None), ("POST", "/api/settings", {"layer": "fake"}),
                                   ("GET", "/api/layers/fake/panel", None),
                                   ("POST", "/api/layers/fake/act", {"action": "x", "args": {}})):
            self.assertEqual(self.call(method, path, body, headers={"Authorization": None})[0], 401, path)
            self.assertEqual(self.call(method, path, body, headers={"Origin": "http://evil.example"})[0], 403, path)
        self.assertEqual(self.fake.acts, [])

    def test_an_action_passes_its_arguments_and_the_settings_to_the_layer(self):
        st, j = self.call("POST", "/api/layers/fake/act", {"action": "go", "args": {"n": 1}})
        self.assertEqual((st, j["message"]), (200, "done"))
        self.assertEqual(self.fake.acts, [("go", {"n": 1}, {"home": "/default"})])

    def test_bad_requests_are_400_and_a_layers_bug_is_500_without_leaking_it_to_the_server(self):
        self.assertEqual(self.call("POST", "/api/layers/fake/act", {"action": "bad", "args": {}})[0], 400)
        self.assertEqual(self.call("POST", "/api/layers/fake/act", {"action": 5, "args": {}})[0], 400)
        self.assertEqual(self.call("POST", "/api/layers/fake/act", {"action": "x", "args": []})[0], 400)
        st, j = self.call("POST", "/api/layers/fake/act", {"action": "boom", "args": {}})
        self.assertEqual(st, 500)
        self.assertEqual(self.call("GET", "/api/settings")[0], 200)  # the server is still there
        self.assertEqual(self.call("GET", "/api/layers/nope/panel")[0], 404)


class TestSuperagentPanel(unittest.TestCase):
    STATUS = {"tickets": [{"id": 3, "state": "waiting", "thread": "t1", "note": "", "run_gen": 1}],
              "open_asks": [{"ask_id": 7, "ticket_id": 3, "what": "run_command: ls"}],
              "outbox": {"pending": 1}, "running": True}

    def layer(self, reply):
        self.rec = Recorder(reply)
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "superagent"))
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return SuperagentLayer(src=d, runner=self.rec)

    def test_the_panel_shows_what_waits_for_a_person_with_a_button_per_choice(self):
        layer = self.layer((0, json.dumps(self.STATUS), ""))
        p = layer.panel({"home": "H"})
        self.assertEqual(p["status"], "orchestrator 執行中")
        ask = next(b for b in p["blocks"] if b["type"] == "list")["items"][0]
        self.assertEqual([(b["action"], b["args"]) for b in ask["buttons"]],
                         [("answer", {"ask_id": 7, "yes": True}), ("answer", {"ask_id": 7, "yes": False})])
        self.assertEqual(self.rec.calls[0][:4], ["-m", "superagent", "--home=H", "status"])

    def test_a_button_is_offered_only_where_it_means_something(self):
        """The engine's `resume` reopens any ticket, also one a person stopped 'for good'. A resume button
        on a stopped ticket would be a button that undoes a stop (clicking it against a running
        orchestrator showed exactly that), so resume exists only for a parked ticket and stop only
        for one that can still be stopped."""
        status = dict(self.STATUS, open_asks=[], tickets=[
            {"id": 1, "state": "open", "thread": "a", "note": ""},
            {"id": 2, "state": "waiting", "thread": "b", "note": ""},
            {"id": 3, "state": "stopped", "thread": "c", "note": ""},
            {"id": 4, "state": "ended", "thread": "d", "note": ""},
            {"id": 5, "state": "open", "thread": "e", "note": "parked: the executor died 3 times"},
            {"id": 6, "state": "stopped", "thread": "f", "note": "left over note"}])
        layer = self.layer((0, json.dumps(status), ""))
        items = [b for b in layer.panel({"home": "H"})["blocks"] if b["type"] == "list"][1]["items"]
        offered = {int(i["text"].split()[0][1:]): [b["action"] for b in i["buttons"]] for i in items}
        self.assertEqual(offered, {1: ["stop"], 2: ["stop"], 3: [], 4: [], 5: ["stop", "resume"], 6: []})

    def test_answering_sends_y_or_n_and_never_the_permanent_a(self):
        """A button must not be able to grant 'always allow' (the CLI refuses it, and so must this)."""
        layer = self.layer((0, "answered", ""))
        layer.act({"home": "H"}, "answer", {"ask_id": 7, "yes": True})
        layer.act({"home": "H"}, "answer", {"ask_id": 8, "yes": False})
        self.assertEqual([c[-3:] for c in self.rec.calls], [["answer", "7", "y"], ["answer", "8", "n"]])

    def test_arguments_must_be_whole_numbers_and_known_actions(self):
        layer = self.layer((0, "", ""))
        for action, args in (("answer", {"ask_id": "7", "yes": True}), ("answer", {"ask_id": True, "yes": True}),
                             ("answer", {"ask_id": 7, "yes": "y"}), ("stop", {"ticket": "1; rm -rf"}),
                             ("resume", {"ticket": 1.5}), ("shutdown", {}), ("down", {"mode": "purge"})):
            with self.assertRaises(ValueError, msg=(action, args)):
                layer.act({"home": "H"}, action, args)
        self.assertEqual(self.rec.calls, [])                          # nothing was run

    def test_no_state_yet_is_said_plainly_and_a_failure_is_shown_not_raised(self):
        self.assertEqual(self.layer((1, "no state yet\n", "")).panel({"home": "H"})["status"], "還沒有資料")
        self.assertEqual(self.layer((2, "", "boom")).panel({"home": "H"})["status"], "讀不到狀態")
        self.assertEqual(self.layer((0, "not json", "")).panel({"home": "H"})["status"], "讀不到狀態")

    def test_a_missing_package_is_reported_not_a_crash(self):
        layer = SuperagentLayer(src=tempfile.gettempdir() + "/definitely-not-here", runner=Recorder())
        self.assertEqual(layer.panel({"home": "H"})["status"], "未安裝")
        with self.assertRaises(ValueError):
            layer.act({"home": "H"}, "stop", {"ticket": 1})


class TestKbPanel(unittest.TestCase):
    def layer(self, reply=(0, "", "")):
        self.rec = Recorder(reply)
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "kb", "src", "kb"))
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return KbLayer(root=d, runner=self.rec)

    VALUES = {"home": "KH", "base_url": "http://localhost:8000/v1", "model": "m", "provider": "vllm"}

    def test_a_question_that_looks_like_an_option_stays_a_question(self):
        """Free text goes after `--` and settings use `--name=value`, so '--home=/etc' typed into the
        search box cannot become an option of kb."""
        layer = self.layer((0, "hits", ""))
        layer.act(self.VALUES, "search", {"text": "--home=/etc"})
        layer.act(self.VALUES, "ask", {"text": "-k 99 what?"})
        search, ask = self.rec.calls
        self.assertEqual(search[search.index("--") + 1:], ["--home=/etc"])
        self.assertEqual(ask[ask.index("--") + 1:], ["-k 99 what?"])
        self.assertIn("--base-url=http://localhost:8000/v1", ask)
        self.assertLess(ask.index("--"), len(ask) - 1)

    def test_a_held_answer_is_a_result_to_read_not_an_error(self):
        """`kb ask` exits 4 when it holds an answer back; that is its normal way of saying 'look first'."""
        r = self.layer((4, "answer a1: HELD", "")).act(self.VALUES, "ask", {"text": "q"})
        self.assertTrue(r["ok"])
        self.assertIn("HELD", r["result"])
        self.assertFalse(self.layer((2, "", "no commit")).act(self.VALUES, "ask", {"text": "q"})["ok"])

    def test_ask_needs_an_endpoint_and_search_does_not(self):
        layer = self.layer()
        with self.assertRaises(ValueError):
            layer.act(dict(self.VALUES, base_url=""), "ask", {"text": "q"})
        with self.assertRaises(ValueError):
            layer.act(dict(self.VALUES, base_url="file:///etc/passwd"), "ask", {"text": "q"})
        self.assertEqual(self.rec.calls, [])
        layer.act(dict(self.VALUES, base_url=""), "search", {"text": "q"})
        self.assertEqual(len(self.rec.calls), 1)

    def test_the_ask_box_appears_only_when_it_can_work(self):
        layer = self.layer((0, "state ok", ""))
        forms = lambda v: [b["action"] for b in layer.panel(v)["blocks"] if b["type"] == "form"]
        self.assertEqual(forms(self.VALUES), ["search", "ask"])
        self.assertEqual(forms(dict(self.VALUES, model="")), ["search"])

    def test_release_is_offered_only_for_held_answers_and_asks_first(self):
        listing = "a1  ready     2026-11-20 'q1'\na2  held      2026-11-20 'q2'\n"
        layer = self.layer(lambda args: (0, listing if "answers" in args else "status text", ""))
        items = next(b for b in layer.panel(self.VALUES)["blocks"] if b["type"] == "list")["items"]
        by_id = {i["buttons"][0]["args"]["ask_id"]: [b["action"] for b in i["buttons"]] for i in items}
        self.assertEqual(by_id, {"a1": ["show"], "a2": ["show", "release"]})
        self.assertTrue(items[0]["buttons"][-1].get("confirm"))      # newest first: a2, the held one

    def test_answer_ids_cannot_carry_an_option(self):
        layer = self.layer()
        for bad in ("--by=x", "-1", "", "a b", "../x", 5, None):
            with self.assertRaises(ValueError, msg=bad):
                layer.act(self.VALUES, "show", {"ask_id": bad})
        self.assertEqual(self.rec.calls, [])

    def test_an_absent_kb_is_reported_not_a_crash(self):
        layer = KbLayer(root=tempfile.gettempdir() + "/definitely-not-here", runner=Recorder())
        self.assertEqual(layer.panel({"home": "KH"})["status"], "未安裝")


class TestTheHarnessFolderStillKnowsNothing(unittest.TestCase):
    def test_no_harness_module_mentions_a_layer_as_code(self):
        """superagent's and kb's own tests promise that src/harness/ neither imports nor runs them.
        The settings page works through a generic hook, so this must still hold with the GUI changes."""
        folder = os.path.join(SRC, "harness")
        names = {"superagent", "kb", "gui_layers"}
        found = []
        for fn in sorted(os.listdir(folder)):
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(folder, fn), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    mods = [node.module or ""]
                elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in ("import_module", "__import__") \
                        and node.args and isinstance(node.args[0], ast.Constant):
                    mods = [str(node.args[0].value)]
                elif isinstance(node, ast.List) and len(node.elts) >= 2 and isinstance(node.elts[0], ast.Constant) \
                        and node.elts[0].value == "-m" and isinstance(node.elts[1], ast.Constant):
                    mods = [str(node.elts[1].value)]                 # a command line of the form [..., "-m", name, ...]
                found += [(fn, m) for m in mods if m.split(".")[0] in names]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
