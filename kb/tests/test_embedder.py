"""S3c: the embedding backend: the pinned model (T-X1), the resident server's life cycle, and what is accepted from it."""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import numpy as np

from kb import embedder
from kb.embedder import EmbedError, EmbedderUnavailable, Identity, LlamaServerEmbedder
from kb.errors import VectorUnavailable
from kb.home import Home

from .support import tmpdir

REAL_SERVER = "C:/wcpp/llama.cpp/build/bin/Release/llama-server.exe"
REAL_MODEL = "C:/wcpp/models/Qwen3-Embedding-0.6B-Q8_0.gguf"


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Stub:
    """A tiny local HTTP server that answers like llama-server: /health and /v1/embeddings. `behaviour` picks what it does wrong."""

    def __init__(self):
        self.requests, self.behaviour, self.dim = [], "ok", 4
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append(body)
                if stub.behaviour == "error":
                    self.send_response(500)
                    self.end_headers()
                    return
                texts = body["input"]
                rows = [{"index": i, "embedding": [float(len(t)), 1.0, float(i + 1), 0.5][:stub.dim]} for i, t in enumerate(texts)]
                if stub.behaviour == "shuffled":
                    rows = rows[::-1]
                elif stub.behaviour == "short":
                    rows = rows[:-1]
                elif stub.behaviour == "nan":
                    rows[0]["embedding"][0] = float("nan")
                elif stub.behaviour == "zero":
                    rows[0]["embedding"] = [0.0] * stub.dim
                payload = json.dumps({"data": rows}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _healthy_port(port):
    return embedder._healthy(port)


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir(self)
        self.home = Home(os.path.join(self.d, "h"))
        self.home.init()
        self.model = os.path.join(self.d, "model.gguf")
        with open(self.model, "wb") as f:
            f.write(b"pretend weights")
        self.server = sys.executable
        self.cfg = embedder.pin(self.home, self.server, self.model)

    def stub(self):
        s = Stub()
        self.addCleanup(s.close)
        return s

    def attached(self, stub):
        """An embedder that believes a server is already running on the stub's port."""
        e = LlamaServerEmbedder(self.home)
        os.makedirs(os.path.dirname(e.state_path), exist_ok=True)
        with open(e.state_path, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "port": stub.port, "fingerprint": e._fingerprint()}, f)       # the stub listens inside this very process
        return e


class TestPin(Base):
    def test_pinning_records_the_hash_of_the_model_file_and_the_paths(self):
        cfg = read_json(embedder.config_path(self.home))
        self.assertEqual(cfg["model_sha256"], embedder.sha256_file(self.model))
        self.assertEqual(cfg["model_id"], "model.gguf")
        self.assertEqual(cfg["instruction"], embedder.DEFAULT_INSTRUCTION)
        self.assertIsNone(cfg["dim"])

    def test_a_missing_file_cannot_be_pinned(self):
        for server, model in ((os.path.join(self.d, "nope"), self.model), (self.server, os.path.join(self.d, "nope"))):
            with self.assertRaises(EmbedderUnavailable):
                embedder.pin(self.home, server, model)

    def test_a_replaced_model_file_is_refused_before_anything_is_started(self):
        with open(self.model, "wb") as f:
            f.write(b"different weights!")
        with mock.patch("subprocess.Popen") as popen:
            with self.assertRaises(EmbedderUnavailable) as cm:
                LlamaServerEmbedder(self.home).ensure_running()
        self.assertIn("does not match the pinned hash", str(cm.exception))
        popen.assert_not_called()

    def test_a_file_that_did_not_change_is_not_hashed_again_at_every_start(self):
        e = LlamaServerEmbedder(self.home)
        with mock.patch.object(embedder, "sha256_file", wraps=embedder.sha256_file) as h:
            embedder._verify_model(self.home, e.cfg)
            embedder._verify_model(self.home, e.cfg)
        self.assertEqual(h.call_count, 1)

    def test_a_change_that_keeps_size_and_time_cannot_hide_when_the_size_changes(self):
        e = LlamaServerEmbedder(self.home)
        embedder._verify_model(self.home, e.cfg)
        with open(self.model, "ab") as f:
            f.write(b"x")
        with self.assertRaises(EmbedderUnavailable):
            embedder._verify_model(self.home, e.cfg)

    def test_without_a_configuration_the_backend_is_unavailable_with_the_command_to_fix_it(self):
        os.remove(embedder.config_path(self.home))
        with self.assertRaises(EmbedderUnavailable) as cm:
            LlamaServerEmbedder(self.home)
        self.assertIn("kb embedder set", str(cm.exception))

    def test_an_unreadable_configuration_is_unavailable_not_a_crash(self):
        with open(embedder.config_path(self.home), "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaises(EmbedderUnavailable):
            LlamaServerEmbedder(self.home)


class TestIdentity(unittest.TestCase):
    def test_every_part_of_the_identity_changes_its_key(self):
        base = Identity("m", "a" * 64, 8, "last", "instr")
        keys = {base.key()}
        for other in (Identity("n", "a" * 64, 8, "last", "instr"), Identity("m", "b" * 64, 8, "last", "instr"), Identity("m", "a" * 64, 9, "last", "instr"),
                      Identity("m", "a" * 64, 8, "mean", "instr"), Identity("m", "a" * 64, 8, "last", "other")):
            keys.add(other.key())
        self.assertEqual(len(keys), 6)

    def test_the_same_identity_has_the_same_key(self):
        self.assertEqual(Identity("m", "a" * 64, 8, "last", "i").key(), Identity("m", "a" * 64, 8, "last", "i").key())


class TestRequests(Base):
    def test_vectors_come_back_in_the_order_asked_with_unit_length_even_if_the_server_shuffles(self):
        s = self.stub()
        s.behaviour = "shuffled"
        v = self.attached(s).embed_documents(["a", "bb", "ccc"])
        self.assertEqual(v.shape, (3, 4))
        self.assertTrue(np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-6))
        self.assertTrue(np.allclose(v[0] * np.linalg.norm([1.0, 1.0, 1.0, 0.5]), [1.0, 1.0, 1.0, 0.5], atol=1e-5))     # row 0 belongs to "a"
        self.assertTrue(np.allclose(v[2] * np.linalg.norm([3.0, 1.0, 3.0, 0.5]), [3.0, 1.0, 3.0, 0.5], atol=1e-5))     # row 2 belongs to "ccc"

    def test_a_query_carries_the_instruction_and_a_document_does_not(self):
        s = self.stub()
        e = self.attached(s)
        e.embed_documents(["plain passage"])
        e.embed_query("what is it")
        self.assertEqual(s.requests[0]["input"], ["plain passage"])
        self.assertEqual(s.requests[1]["input"], [f"Instruct: {embedder.DEFAULT_INSTRUCTION} Query:what is it"])

    def test_documents_go_in_batches(self):
        s = self.stub()
        self.attached(s).embed_documents([f"t{i}" for i in range(33)])
        self.assertEqual([len(r["input"]) for r in s.requests], [16, 16, 1])

    def test_a_text_that_cannot_fit_is_refused_without_a_request(self):
        s = self.stub()
        with self.assertRaises(EmbedError):
            self.attached(s).embed_documents(["x" * (embedder.MAX_CHARS + 1)])
        self.assertEqual(s.requests, [])

    def test_a_server_that_answers_wrongly_is_an_embed_error_which_is_also_a_vector_unavailable(self):
        for behaviour in ("error", "short", "nan", "zero"):
            with self.subTest(behaviour=behaviour):
                s = self.stub()
                s.behaviour = behaviour
                with self.assertRaises(EmbedError) as cm:
                    self.attached(s).embed_documents(["a", "b"])
                self.assertIsInstance(cm.exception, VectorUnavailable)

    def test_the_dimension_is_learned_from_the_first_answer_and_saved(self):
        s = self.stub()
        e = self.attached(s)
        ident = e.identity
        self.assertEqual(ident.dim, 4)
        self.assertEqual(read_json(embedder.config_path(self.home))["dim"], 4)
        self.assertEqual(ident.model_sha256, embedder.sha256_file(self.model))

    def test_an_empty_list_gives_an_empty_matrix_without_a_request(self):
        s = self.stub()
        self.assertEqual(self.attached(s).embed_documents([]).shape[0], 0)
        self.assertEqual(s.requests, [])


class TestLifeCycle(Base):
    def test_a_running_server_is_reused_not_started_again(self):
        s = self.stub()
        e = self.attached(s)
        with mock.patch.object(LlamaServerEmbedder, "_start") as start:
            self.assertEqual(e.ensure_running(), s.port)
        start.assert_not_called()

    def test_a_server_that_exits_at_start_is_reported_with_where_to_look(self):
        e = LlamaServerEmbedder(self.home)                       # the "server" is the Python interpreter, which rejects these arguments at once
        with self.assertRaises(EmbedderUnavailable) as cm:
            e.ensure_running()
        self.assertIn("exited at start", str(cm.exception))
        self.assertIn("embedder.log", str(cm.exception))

    def test_a_server_that_never_becomes_ready_is_stopped_and_reported(self):
        e = LlamaServerEmbedder(self.home)
        proc = mock.Mock(pid=4242)
        proc.poll.return_value = None
        with mock.patch.object(embedder, "START_TIMEOUT_S", 0.3), mock.patch("subprocess.Popen", return_value=proc), \
                mock.patch.object(LlamaServerEmbedder, "_kill") as kill:
            with self.assertRaises(EmbedderUnavailable) as cm:
                e.ensure_running()
        self.assertIn("did not become ready", str(cm.exception))
        kill.assert_called_once_with(4242)

    def test_a_started_server_is_remembered_so_the_next_command_finds_it(self):
        s = self.stub()
        e = LlamaServerEmbedder(self.home)
        proc = mock.Mock(pid=777)
        proc.poll.return_value = None
        with mock.patch("subprocess.Popen", return_value=proc) as popen, mock.patch.object(embedder, "_free_port", return_value=s.port):
            port = e.ensure_running()
        self.assertEqual(port, s.port)
        args = popen.call_args[0][0]
        self.assertEqual(args[:3], [self.server, "-m", self.model])
        self.assertIn("127.0.0.1", args)                              # never listens on another interface
        self.assertEqual(args[args.index("-np") + 1], "1")
        self.assertEqual(args[args.index("-c") + 1], "4096")
        state = read_json(e.state_path)
        self.assertEqual((state["pid"], state["port"]), (777, s.port))
        with mock.patch("subprocess.Popen") as again, mock.patch.object(embedder, "_listener_pid", return_value=777):
            self.assertEqual(LlamaServerEmbedder(self.home).ensure_running(), s.port)
        again.assert_not_called()

    def test_stop_ends_only_a_server_that_still_answers_on_the_recorded_port(self):
        s = self.stub()
        e = self.attached(s)
        with mock.patch.object(LlamaServerEmbedder, "_kill") as kill:
            self.assertTrue(e.stop())
            kill.assert_called_once_with(os.getpid())
        self.assertFalse(os.path.exists(e.state_path))
        dead = self.attached(s)
        s.close()
        with mock.patch.object(LlamaServerEmbedder, "_kill") as kill:
            self.assertFalse(dead.stop())                               # nothing answers there: the recorded pid may belong to something else now
            kill.assert_not_called()
        self.assertFalse(os.path.exists(dead.state_path))

    def test_stop_leaves_alone_a_port_that_belongs_to_another_process(self):
        """A health answer proves that something listens, not that it is the recorded process: ports and process ids are recycled."""
        s = self.stub()
        e = self.attached(s)
        state = read_json(e.state_path)
        state["pid"] = os.getpid() + 1                              # the recorded process is not the one that listens on the recorded port
        with open(e.state_path, "w", encoding="utf-8") as f:
            json.dump(state, f)
        with mock.patch.object(LlamaServerEmbedder, "_kill") as kill:
            self.assertFalse(e.stop())
            kill.assert_not_called()
        self.assertTrue(_healthy_port(s.port))                       # and the other server still answers
        self.assertFalse(os.path.exists(e.state_path))

    def test_a_hung_server_that_is_proved_to_be_ours_is_ended_too(self):
        """The proof of ownership is the listener on the port, not an answer: a server that has stopped answering must not be unstoppable."""
        s = self.stub()
        e = self.attached(s)
        with mock.patch.object(embedder, "_healthy", return_value=False), mock.patch.object(LlamaServerEmbedder, "_kill") as kill:
            self.assertTrue(e.stop())
            kill.assert_called_once_with(os.getpid())

    def test_a_server_started_with_other_flags_is_not_reused(self):
        s = self.stub()
        e = self.attached(s)
        new = self.stub()
        proc = mock.Mock(pid=556)
        proc.poll.return_value = None
        with mock.patch.object(embedder, "SERVER_FLAGS", embedder.SERVER_FLAGS + ["--some-new-flag"]), mock.patch("subprocess.Popen", return_value=proc) as popen, \
                mock.patch.object(embedder, "_free_port", return_value=new.port), mock.patch.object(embedder, "_listener_pid", return_value=os.getpid()), \
                mock.patch.object(LlamaServerEmbedder, "_kill"):
            self.assertEqual(LlamaServerEmbedder(self.home).ensure_running(), new.port)
        popen.assert_called_once()
        self.assertIn("--some-new-flag", popen.call_args[0][0])
        self.assertTrue(e is not None)

    def test_stop_ends_nothing_when_the_owner_of_the_port_cannot_be_told(self):
        s = self.stub()
        e = self.attached(s)
        with mock.patch.object(embedder, "_listener_pid", return_value=None), mock.patch.object(LlamaServerEmbedder, "_kill") as kill:
            self.assertFalse(e.stop())
            kill.assert_not_called()

    def test_the_listener_of_a_port_is_found(self):
        s = self.stub()
        self.assertEqual(embedder._listener_pid(s.port), os.getpid())
        s.close()
        self.assertIsNone(embedder._listener_pid(s.port))

    @unittest.skipUnless(os.name == "nt", "the netstat listing is a Windows format")
    def test_the_netstat_listing_is_read_even_when_its_headers_are_not_ascii_and_the_text_mode_is_utf8(self):
        """Found by a real run under PYTHONUTF8=1: the localized headers of netstat are not valid UTF-8, and decoding them in text mode left no output at all."""
        listing = ("使用中連線".encode("cp950") + b"\r\n  TCP    127.0.0.1:52044        127.0.0.1:60001        ESTABLISHED     4242\r\n"      # a client of the server, not the listener
                   b"  TCP    127.0.0.1:52044        0.0.0.0:0              " + "LISTENING".encode("ascii") + b"       36760\r\n"
                   b"  TCP    127.0.0.1:52045        0.0.0.0:0              LISTENING       99\r\n")
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout=listing)):
            self.assertEqual(embedder._listener_pid(52044), 36760)
            self.assertIsNone(embedder._listener_pid(40000))

    def test_a_server_started_for_another_pin_is_not_reused(self):
        """Pinning a different model leaves the old server answering with the old model's vectors; they must never carry the new model's identity."""
        old = self.stub()
        e_old = self.attached(old)
        with open(self.model, "wb") as f:
            f.write(b"another set of weights")
        embedder.pin(self.home, self.server, self.model)             # approved: a new hash
        new = self.stub()
        proc = mock.Mock(pid=555)
        proc.poll.return_value = None
        e = LlamaServerEmbedder(self.home)
        with mock.patch("subprocess.Popen", return_value=proc) as popen, mock.patch.object(embedder, "_free_port", return_value=new.port),                 mock.patch.object(embedder, "_listener_pid", return_value=os.getpid()), mock.patch.object(LlamaServerEmbedder, "_kill") as kill:
            self.assertEqual(e.ensure_running(), new.port)
        popen.assert_called_once()
        kill.assert_called_once_with(os.getpid())                    # the old server (proved to be the recorded one) was ended
        state = read_json(e.state_path)
        self.assertEqual(state["fingerprint"]["model_sha256"], embedder.sha256_file(self.model))
        self.assertEqual(state["port"], new.port)
        self.assertNotEqual(e_old._fingerprint(), e._fingerprint())

    def test_a_stranger_on_the_recorded_port_is_not_used_even_when_the_state_looks_right(self):
        """The port was recycled: something healthy answers there, the fingerprint in the file is the right one, but the process is not the recorded one."""
        stranger = self.stub()
        e = self.attached(stranger)
        state = read_json(e.state_path)
        state["pid"] = os.getpid() + 1
        with open(e.state_path, "w", encoding="utf-8") as f:
            json.dump(state, f)
        new = self.stub()
        proc = mock.Mock(pid=555)
        proc.poll.return_value = None
        with mock.patch("subprocess.Popen", return_value=proc) as popen, mock.patch.object(embedder, "_free_port", return_value=new.port), \
                mock.patch.object(embedder, "_listener_pid", return_value=os.getpid()), mock.patch.object(LlamaServerEmbedder, "_kill") as kill:
            self.assertEqual(LlamaServerEmbedder(self.home).ensure_running(), new.port)
        popen.assert_called_once()
        kill.assert_not_called()                                                       # and the stranger is left alone

    def test_a_server_of_another_pin_that_cannot_be_proved_ours_is_not_ended_but_not_used_either(self):
        old = self.stub()
        self.attached(old)
        with open(self.model, "wb") as f:
            f.write(b"another set of weights")
        embedder.pin(self.home, self.server, self.model)
        new = self.stub()
        proc = mock.Mock(pid=555)
        proc.poll.return_value = None
        with mock.patch("subprocess.Popen", return_value=proc) as popen, mock.patch.object(embedder, "_free_port", return_value=new.port),                 mock.patch.object(embedder, "_listener_pid", return_value=None), mock.patch.object(LlamaServerEmbedder, "_kill") as kill:
            self.assertEqual(LlamaServerEmbedder(self.home).ensure_running(), new.port)
        popen.assert_called_once()
        kill.assert_not_called()

    def test_status_says_whether_it_runs(self):
        s = self.stub()
        self.assertEqual(self.attached(s).status()["running"], True)
        self.assertEqual(LlamaServerEmbedder(self.home).status()["running"], True)       # the state file is shared by every command of the same home
        os.remove(LlamaServerEmbedder(self.home).state_path)
        self.assertEqual(LlamaServerEmbedder(self.home).status()["running"], False)


@unittest.skipUnless(os.path.isfile(REAL_SERVER) and os.path.isfile(REAL_MODEL), "the local llama.cpp server and model are not installed")
class TestRealBackend(unittest.TestCase):
    """One run against the real server and model: the resident process starts, embeds, is found again by a second embedder and stops."""

    def test_start_embed_reuse_stop(self):
        d = tmpdir(self)
        home = Home(os.path.join(d, "h"))
        home.init()
        embedder.pin(home, REAL_SERVER, REAL_MODEL)
        e = LlamaServerEmbedder(home)

        def end_whatever_this_test_started():
            st = e._read_state()
            e.stop()
            if st and embedder._healthy(st["port"]):             # kb's own stop leaves a server it cannot prove to be its own (for instance one
                LlamaServerEmbedder._kill(st["pid"])              # bound to another address by a changed flag); this test started it, so it ends it
        self.addCleanup(end_whatever_this_test_started)
        ident = e.identity
        self.assertEqual(ident.dim, 1024)
        docs = e.embed_documents(["The falcon flies over the harbor.", "Quarterly tax filing deadline in April."])
        q = e.embed_query("bird near the port")
        self.assertTrue(np.allclose(np.linalg.norm(docs, axis=1), 1.0, atol=1e-4))
        self.assertGreater(float(docs[0] @ q), float(docs[1] @ q))                       # the bird passage is nearer than the tax passage
        again = LlamaServerEmbedder(home)
        with mock.patch.object(LlamaServerEmbedder, "_start") as start:
            again.ensure_running()
        start.assert_not_called()
        self.assertTrue(e.stop())
        self.assertFalse(LlamaServerEmbedder(home).status()["running"])


if __name__ == "__main__":
    unittest.main()
