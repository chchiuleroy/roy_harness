"""Tests for view_image: a picture goes to the model without ever being stored in the session.

The design under test: the tool result is text; the picture arrives in the NEXT message as a
reference ({type: image_ref, path, sha256}) that lives in the session file; the bytes are read
from disk and put on the wire only when a request is built, and only for the latest few. So the
session stays small, a compaction can drop old pictures, and a changed file is noticed.

Run:  python -X utf8 -m unittest discover -s tests -v
Each test's docstring says WHY the behaviour matters, not just what it is.
"""
import base64
import hashlib
import http.server
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src", "harness")
sys.path.insert(0, SRC)
import harness_vllm as hv  # noqa: E402


def png(w=4, h=3, color=(220, 30, 30)):
    """A real, tiny PNG (stdlib only): enough for a server to decode."""
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    raw = b"".join(b"\x00" + bytes(color) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def gif(w, h):
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00\x00\x00;"


def jpeg(w, h):
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def webp_vp8x(w, h):
    return (b"RIFF" + struct.pack("<I", 22) + b"WEBP" + b"VP8X" + struct.pack("<I", 10)
            + b"\x00\x00\x00\x00" + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little"))


def webp_vp8l(w, h):
    bits = (w - 1) | ((h - 1) << 14)
    return (b"RIFF" + struct.pack("<I", 17) + b"WEBP" + b"VP8L" + struct.pack("<I", 5)
            + b"\x2f" + bits.to_bytes(4, "little"))


def webp_vp8(w, h):
    return (b"RIFF" + struct.pack("<I", 22) + b"WEBP" + b"VP8 " + struct.pack("<I", 10)
            + b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", w, h))


class Base(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self._saved = (hv.VISION, hv.IMAGES_IN_PROMPT, hv.MAX_IMAGE_BYTES, hv.MAX_IMAGE_PIXELS)
        hv.VISION = True
        hv._IMAGES.clear()

    def tearDown(self):
        hv.VISION, hv.IMAGES_IN_PROMPT, hv.MAX_IMAGE_BYTES, hv.MAX_IMAGE_PIXELS = self._saved
        hv._CALL.key = None
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def write(self, name, data):
        with open(name, "wb") as f:
            f.write(data)
        return os.path.abspath(name)


class TestSniffing(Base):
    def test_every_supported_format_is_recognised_from_its_bytes(self):
        """The extension is a claim, the bytes are the fact: size and kind come from the header."""
        cases = [(png(7, 5), "image/png", 7, 5), (gif(9, 4), "image/gif", 9, 4),
                 (jpeg(11, 6), "image/jpeg", 11, 6), (webp_vp8x(13, 8), "image/webp", 13, 8),
                 (webp_vp8l(15, 10), "image/webp", 15, 10), (webp_vp8(17, 12), "image/webp", 17, 12)]
        for data, mime, w, h in cases:
            self.assertEqual(hv.sniff_image(data), (mime, w, h), mime)

    def test_things_that_are_not_images_are_refused(self):
        """A text file named .png, a truncated header or a zero-size image must not reach a model."""
        for data in (b"hello, I am text", b"", png()[:20], b"GIF89a\x00", b"\xff\xd8\xff\xe0",
                     gif(0, 5), b"RIFF\x00\x00\x00\x00WEBPJUNK" + b"\x00" * 20):
            self.assertIsNone(hv.sniff_image(data), data[:12])


class TestViewImageTool(Base):
    def run_tool(self, path, call_id="c1"):
        hv._CALL.key = ("t", call_id)
        return hv.execute_tool("view_image", {"path": path})

    def test_a_good_image_returns_text_and_registers_the_picture(self):
        path = self.write("pic.png", png(4, 3))
        out = self.run_tool(path)
        self.assertIn("pic.png", out)
        self.assertIn("4x3", out)
        part = hv._IMAGES[("t", "c1")]
        self.assertEqual(part["type"], "image_ref")
        self.assertEqual(part["sha256"], hashlib.sha256(png(4, 3)).hexdigest())
        self.assertNotIn("base64", json.dumps(part))               # a reference, never the bytes

    def test_refusals_say_why_and_register_nothing(self):
        """Each refusal is an error the model can act on, and none leaves a picture behind."""
        hv.MAX_IMAGE_BYTES, hv.MAX_IMAGE_PIXELS = 1000, 100
        cases = {"text.png": b"just words", "big.png": png(4, 3) + b"\x00" * 2000,
                 "wide.png": png(20, 20)}
        for name, data in cases.items():
            out = self.run_tool(self.write(name, data))
            self.assertTrue(out.startswith("ERROR"), (name, out))
        self.assertTrue(self.run_tool("missing.png").startswith("ERROR"))
        self.assertEqual(hv._IMAGES, {})
        self.assertIn("pixels", self.run_tool(self.write("wide2.png", png(20, 20))))

    def test_without_vision_the_tool_is_not_offered_and_refuses(self):
        """A server that cannot take images must not be handed the tool in the first place."""
        hv.VISION = False
        self.assertNotIn("view_image", [t["name"] for t in hv.tools_for(0)])
        self.assertNotIn("view_image", [t["name"] for t in hv.tools_for(1)])
        self.assertTrue(self.run_tool(self.write("p.png", png())).startswith("ERROR"))
        hv.VISION = True
        self.assertIn("view_image", [t["name"] for t in hv.tools_for(0)])
        self.assertIn("view_image", [t["name"] for t in hv.tools_for(1)])

    def test_a_hosted_provider_asks_before_a_picture_leaves_the_machine(self):
        """Pictures (screenshots!) can hold secrets; a local server is the user's own, a hosted one is not."""
        path = self.write("p.png", png())
        saved = hv.PROVIDER, hv.read_line
        hv.PROVIDER = "openai"
        asked = []
        hv.read_line = lambda prompt: (asked.append(prompt), "n")[1]
        try:
            out = self.run_tool(path)
        finally:
            hv.PROVIDER, hv.read_line = saved
        self.assertEqual(len(asked), 1)
        self.assertTrue(out.startswith("DENIED"), out)
        self.assertEqual(hv._IMAGES, {})


class TestWire(Base):
    """expand_images: the only place where bytes leave the disk."""

    def ref(self, path):
        data = open(path, "rb").read()
        mime, w, h = hv.sniff_image(data)
        return {"type": "image_ref", "path": path, "name": os.path.basename(path), "mime": mime,
                "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "width": w, "height": h}

    def msgs(self, paths):
        out = []
        for p in paths:
            out += [{"role": "user", "content": "look"},
                    {"role": "user", "content": [{"type": "text", "text": "attached"}, self.ref(p)]}]
        return out

    def test_the_wire_gets_data_uris_and_never_the_reference(self):
        p = self.write("a.png", png(4, 3))
        sent = hv.expand_images(self.msgs([p]))
        part = sent[1]["content"][1]
        self.assertEqual(part["type"], "image_url")
        self.assertEqual(part["image_url"]["url"], "data:image/png;base64," + base64.b64encode(png(4, 3)).decode())
        self.assertNotIn("image_ref", json.dumps(sent))

    def test_the_original_messages_are_not_changed(self):
        """expand_images builds the request; the saved conversation keeps its small references."""
        p = self.write("a.png", png())
        msgs = self.msgs([p])
        before = json.dumps(msgs, sort_keys=True)
        hv.expand_images(msgs)
        self.assertEqual(json.dumps(msgs, sort_keys=True), before)

    def test_only_the_latest_pictures_are_sent_again(self):
        """Each picture costs thousands of tokens on every later request; old ones become a note."""
        hv.IMAGES_IN_PROMPT = 2
        paths = [self.write(f"{i}.png", png(4, 3, (i * 40, 10, 10))) for i in range(4)]
        sent = hv.expand_images(self.msgs(paths))
        parts = [p for m in sent if isinstance(m["content"], list) for p in m["content"]]
        self.assertEqual(sum(p["type"] == "image_url" for p in parts), 2)
        notes = [p["text"] for m in sent if isinstance(m["content"], list) for p in m["content"]
                 if "left out to save context" in p.get("text", "")]
        self.assertEqual(len(notes), 2)
        self.assertIn("0.png", notes[0])                            # the oldest are the ones left out

    def test_a_changed_or_missing_file_is_a_note_not_a_wrong_picture(self):
        """The model was told 'this is a.png'; if the file is different now, sending it would lie."""
        p = self.write("a.png", png(4, 3))
        msgs = self.msgs([p])
        self.write("a.png", png(4, 3, (0, 255, 0)))                 # same name, other picture
        text = json.dumps(hv.expand_images(msgs))
        self.assertNotIn("image_url", text)
        self.assertIn("a.png", text)
        os.remove(p)
        text = json.dumps(hv.expand_images(msgs))
        self.assertNotIn("image_url", text)
        self.assertIn("could not be loaded", text)

    def test_text_only_conversations_pass_through_untouched(self):
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
        self.assertIs(hv.expand_images(msgs), msgs)


class TestReviewFindings(Base):
    """What the first review of view_image found (all five were real)."""

    def ref_for(self, path):
        data = open(path, "rb").read()
        mime, w, h = hv.sniff_image(data)
        return {"type": "image_ref", "path": path, "name": os.path.basename(path), "mime": mime,
                "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "width": w, "height": h}

    def test_a_hosted_provider_only_gets_pictures_that_were_approved_in_this_run(self):
        """The question is asked when view_image attaches a picture; a session that was made with
        a local server and is resumed against a hosted one must not send its old pictures
        without a fresh yes (a screenshot can hold secrets)."""
        path = self.write("old.png", png())
        msgs = [{"role": "user", "content": [{"type": "text", "text": "a"}, self.ref_for(path)]}]
        saved = hv.PROVIDER, hv.read_line, set(hv._APPROVED)
        hv.PROVIDER = "openai"
        hv._APPROVED.clear()
        try:
            text = json.dumps(hv.expand_images(msgs))
            self.assertNotIn("image_url", text)
            self.assertIn("not sent", text)
            hv.read_line = lambda prompt: "y"                      # the person says yes once
            hv._CALL.key = ("t", "c1")
            hv.execute_tool("view_image", {"path": path})
            self.assertIn("image_url", json.dumps(hv.expand_images(msgs)))
        finally:
            hv.PROVIDER, hv.read_line = saved[0], saved[1]
            hv._APPROVED.clear()
            hv._APPROVED.update(saved[2])

    def test_two_batches_with_the_same_call_id_do_not_swap_pictures(self):
        """Parallel subagents each have their own messages but the server's call ids need not
        be unique across them; a picture belongs to its own batch, not to an id."""
        a = self.write("a.png", png(4, 3, (255, 0, 0)))
        b = self.write("b.png", png(5, 3, (0, 255, 0)))
        call = lambda path: [{"id": "same", "type": "function", "function": {          # noqa: E731
            "name": "view_image", "arguments": json.dumps({"path": path})}}]
        batch_a, batch_b = call(a), call(b)
        hv.run_calls(batch_a, 1, "s1 ")
        hv.run_calls(batch_b, 1, "s2 ")
        self.assertEqual(hv.take_image((id(batch_a), 0))["name"], "a.png")
        self.assertEqual(hv.take_image((id(batch_b), 0))["name"], "b.png")

    def test_a_crash_after_the_result_is_saved_still_keeps_the_picture(self):
        """The tool result says 'attached to the next message'. If the process dies after that
        result is on disk but before the picture message is added, the session would claim
        something the model never gets. Both are written down together."""
        path = self.write("shot.png", png(8, 6))
        batch = [{"id": "c1", "type": "function", "function": {
                      "name": "view_image", "arguments": json.dumps({"path": path})}},
                 {"id": "c2", "type": "function", "function": {
                      "name": "run_command", "arguments": json.dumps({"command": "echo boom"})}}]
        reply = {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "", "tool_calls": batch}}], "usage": {}}

        class Crash(BaseException):
            pass

        def die(*a, **k):
            raise Crash()
        saved = hv.call_llm, hv.run_shell, hv.SESSION_FILE
        hv.call_llm, hv.run_shell, hv.SESSION_FILE = (lambda *a, **k: reply), die, "session.json"
        try:
            messages = [{"role": "user", "content": "look"}]
            with self.assertRaises(Crash):
                hv._run_loop(messages, hv.tools_for(0), "sys", True, 0, "")
        finally:
            hv.call_llm, hv.run_shell, hv.SESSION_FILE = saved
        reloaded, repaired = hv.load_session("session.json")
        self.assertEqual(repaired, 1)                              # c2 never answered: INTERRUPTED
        self.assertEqual([m["role"] for m in reloaded], ["user", "assistant", "tool", "tool", "user"])
        self.assertEqual(reloaded[-1]["content"][1]["type"], "image_ref")
        self.assertEqual(reloaded[-1]["content"][1]["name"], "shot.png")

    def test_a_malformed_picture_reference_becomes_a_note_not_a_crash(self):
        """An old, hand-edited or damaged session must still produce a request."""
        good = self.write("a.png", png())
        for bad in ({"type": "image_ref", "path": None, "mime": "image/png", "sha256": "x"},
                    {"type": "image_ref", "path": 12, "mime": "image/png", "sha256": "x"},
                    {"type": "image_ref", "path": good, "mime": ["image/png"], "sha256": "x"},
                    {"type": "image_ref"}):
            msgs = [{"role": "user", "content": [bad]}]
            text = json.dumps(hv.expand_images(msgs))
            self.assertNotIn("image_url", text, bad)
            self.assertIn("could not be loaded", text, bad)

    def test_a_cut_off_file_is_not_accepted_as_a_picture(self):
        """Headers can be fine while the rest is missing (an interrupted download). The server
        would refuse the whole request, and the reference would keep failing every later one."""
        for name, data in (("png", png(40, 30)), ("gif", gif(9, 4)), ("jpeg", jpeg(11, 6)),
                           ("webp-x", webp_vp8x(13, 8)), ("webp-l", webp_vp8l(15, 10)),
                           ("webp", webp_vp8(17, 12))):
            self.assertIsNotNone(hv.sniff_image(data), name)
            cut = data[:-1] if name in ("png", "gif", "jpeg") else data[:-2]
            self.assertIsNone(hv.sniff_image(cut), name)


class TestCompactionAndTranscript(Base):
    def test_the_summary_transcript_names_pictures_instead_of_crashing(self):
        """A conversation that holds a picture still has to be compactable and summarisable."""
        p = self.write("a.png", png())
        part = {"type": "image_ref", "path": p, "name": "a.png", "mime": "image/png",
                "sha256": "x", "bytes": 1, "width": 4, "height": 3}
        text = hv.render_transcript([{"role": "user", "content": [{"type": "text", "text": "attached"}, part]}])
        self.assertIn("[image: a.png]", text)
        self.assertIn("attached", text)
        self.assertNotIn("base64", text)


class FakeLLM(http.server.BaseHTTPRequestHandler):
    script, bodies, reject_images = [], [], False

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(n)
        type(self).bodies.append(json.loads(raw))
        if type(self).reject_images and b"image_url" in raw:       # a server that cannot decode it
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "cannot decode image"}')
            return
        body = json.dumps(type(self).script.pop(0)).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)


def text_reply(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {}}


def view_reply(path, cid="c1"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": cid, "type": "function",
            "function": {"name": "view_image", "arguments": json.dumps({"path": path})}}]}}], "usage": {}}


class TestEndToEnd(Base):
    """A real harness process against a scripted server: what is sent, and what is saved."""

    def setUp(self):
        super().setUp()
        FakeLLM.script, FakeLLM.bodies, FakeLLM.reject_images = [], [], False
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        super().tearDown()

    def run_harness(self, stdin_text, *extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("VLLM_", "HARNESS_"))}
        env.update(HARNESS_BASE_URL=f"http://127.0.0.1:{self.srv.server_address[1]}/v1",
                   HARNESS_STREAM="0", HARNESS_MODEL="fake", PYTHONUTF8="1")
        return subprocess.run(
            [sys.executable, "-X", "utf8", os.path.join(SRC, "harness_vllm.py"), *extra, "session.json"],
            input=stdin_text, capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)

    def images_on_wire(self, body):
        return [p for m in body["messages"] if isinstance(m.get("content"), list)
                for p in m["content"] if p.get("type") == "image_url"]

    def test_the_model_gets_the_picture_and_the_session_keeps_only_a_reference(self):
        path = self.write("shot.png", png(8, 6))
        FakeLLM.script = [view_reply(path), text_reply("a red rectangle")]
        out = self.run_harness("what is in shot.png?\n")
        self.assertIn("a red rectangle", out.stdout, out.stdout + out.stderr)
        first, second = FakeLLM.bodies
        self.assertIn("view_image", [t["function"]["name"] for t in first["tools"]])
        self.assertEqual(self.images_on_wire(first), [])            # nothing to see before the tool ran
        wire = self.images_on_wire(second)
        self.assertEqual(len(wire), 1)
        self.assertEqual(wire[0]["image_url"]["url"], "data:image/png;base64," + base64.b64encode(png(8, 6)).decode())
        roles = [m["role"] for m in second["messages"]]
        self.assertEqual(roles[-3:], ["assistant", "tool", "user"])  # the picture follows ALL tool results
        saved = open("session.json", encoding="utf-8").read()
        self.assertNotIn("base64", saved)
        self.assertNotIn("data:image", saved)
        self.assertIn('"image_ref"', saved)
        self.assertLess(len(saved), 3000)                           # a reference, not the picture
        self.assertIn("[image] shot.png 8x6", out.stdout)           # the terminal says so too

    def test_a_resumed_session_sends_the_picture_again_from_disk(self):
        """The reference survives a restart; the bytes are read again when the next request is built."""
        path = self.write("shot.png", png(8, 6))
        FakeLLM.script = [view_reply(path), text_reply("red"), text_reply("still red")]
        self.run_harness("look\n")
        out = self.run_harness("and now?\n")
        self.assertIn("still red", out.stdout, out.stdout + out.stderr)
        self.assertEqual(len(self.images_on_wire(FakeLLM.bodies[-1])), 1)

    def test_a_server_that_refuses_the_picture_does_not_end_the_session(self):
        """A picture the server cannot decode would otherwise fail every later request too (the
        reference stays in the session). One retry without pictures, then notes from then on."""
        path = self.write("shot.png", png(8, 6))
        FakeLLM.reject_images = True
        FakeLLM.script = [view_reply(path), text_reply("I could not see it, but here I am")]
        out = self.run_harness("look\n")
        self.assertIn("here I am", out.stdout, out.stdout + out.stderr)
        self.assertIn("rejected", out.stdout)
        self.assertEqual(len(FakeLLM.bodies), 3)                    # asked, refused, asked again without
        self.assertEqual(self.images_on_wire(FakeLLM.bodies[-1]), [])
        self.assertIn("not sent", json.dumps(FakeLLM.bodies[-1]))

    def test_jsonl_mode_announces_the_picture_as_an_event(self):
        """The GUI needs to know a picture was attached; it gets a structured event, not text to parse."""
        path = self.write("shot.png", png(8, 6))
        FakeLLM.script = [view_reply(path), text_reply("red")]
        out = self.run_harness(json.dumps({"type": "user", "text": "look"}) + "\n", "--jsonl")
        events = [json.loads(line) for line in out.stdout.splitlines() if line.startswith("{")]
        image = [e for e in events if e["kind"] == "image"]
        self.assertEqual(len(image), 1, out.stdout + out.stderr)
        self.assertEqual((image[0]["name"], image[0]["width"], image[0]["height"]), ("shot.png", 8, 6))
        self.assertNotIn("base64", json.dumps(image[0]))


if __name__ == "__main__":
    unittest.main()
