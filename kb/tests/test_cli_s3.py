"""S3d: `kb open` (T-H6 for PDF: the target handed to the viewer is the file and the page of the hit) and the embedding commands end to end."""
import io
import json
import os
import re
import unittest
from contextlib import redirect_stderr
from unittest import mock

from kb import cli, conv_registry, embedder
from kb.home import Home

from .support import tmpdir
from .test_embedder import Stub, read_json
from .test_pdf import TWO_PAGES, Registered, block, make_pdf


class Cli(Registered):
    def setUp(self):
        super().setUp()
        self.d = tmpdir(self)
        self.home_path = os.path.join(self.d, "h")
        self.src = os.path.join(self.d, "src")
        os.makedirs(self.src)

    def write(self, name, data):
        path = os.path.join(self.src, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(["--home", self.home_path, *argv], out=out)
        return code, out.getvalue(), err.getvalue()

    def chunk_id(self, query):
        code, out, _ = self.run_cli("search", query, "--chunks", "--json", "--mode", "lexical")
        return json.loads(out)[0]["chunk_id"]


class TestOpen(Cli):
    def setUp(self):
        super().setUp()
        self.pdf = self.write("falcon.pdf", TWO_PAGES)
        self.run_cli("init")

    def add_and_commit(self, *flags):
        self.run_cli("add", self.pdf, "--settle", "0", *flags)
        self.assertEqual(self.run_cli("commit")[0], 0)

    def test_a_hit_is_opened_at_its_page_and_the_file_url_carries_that_page(self):
        self.add_and_commit()
        code, out, _ = self.run_cli("open", self.chunk_id("quantization sensitivity"), "--json")
        self.assertEqual(code, 0)
        target = json.loads(out)
        self.assertEqual((target["page"], target["status"], target["name"]), (2, "ok", "falcon.pdf"))
        self.assertTrue(target["url"].endswith("falcon.pdf#page=2"), target["url"])
        self.assertEqual(len(target["bbox"]), 4)
        self.assertTrue(os.path.isfile(target["path"]))

    def test_a_stored_copy_is_given_its_real_file_name_so_a_viewer_can_open_it(self):
        self.add_and_commit()
        target = json.loads(self.run_cli("open", self.chunk_id("falcon harbor"), "--json")[1])
        self.assertEqual(target["how"], "stored copy")
        self.assertEqual(os.path.basename(target["path"]), "falcon.pdf")
        with open(target["path"], "rb") as f:
            self.assertEqual(f.read(), TWO_PAGES)                                  # byte for byte what was indexed

    def rewrite(self, *flags):
        self.write("falcon.pdf", make_pdf([[block(80, "A completely different single page about orchards.")]]))
        self.run_cli("add", self.pdf, "--settle", "0", *flags)                     # a new revision is observed; nothing is committed yet

    def test_a_hit_opens_the_file_that_was_indexed_not_a_newer_revision_the_commit_has_not_seen(self):
        self.add_and_commit()
        old = self.chunk_id("falcon harbor")
        self.rewrite()
        code, out, _ = self.run_cli("open", old, "--json")
        target = json.loads(out)
        self.assertEqual((code, target["status"], target["page"]), (0, "ok", 1))
        with open(target["path"], "rb") as f:
            self.assertEqual(f.read(), TWO_PAGES)                                    # the bytes the hit was found in, not the rewritten file

    def test_the_page_range_is_that_of_the_indexed_revision_not_of_a_newer_one(self):
        """The old hit is on page 2 of a two-page file; the file has since become a one-page file whose conversion is already in the database."""
        self.add_and_commit()
        old = self.chunk_id("quantization sensitivity")
        self.rewrite()
        self.assertEqual(self.run_cli("process")[0], 0)                             # the newer revision is converted: its page count is 1
        code, out, err = self.run_cli("open", old, "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["page"], 2)

    def test_a_hit_whose_original_changed_in_place_is_reported_changed_not_opened(self):
        self.add_and_commit("--ref")
        old = self.chunk_id("falcon harbor")
        self.rewrite("--ref")
        code, out, err = self.run_cli("open", old, "--json")
        self.assertEqual((code, json.loads(out)["status"]), (3, "source_changed"))
        self.assertIn("source changed", err)

    def test_a_file_kept_where_it_is_is_opened_where_it_is(self):
        self.add_and_commit("--ref")
        target = json.loads(self.run_cli("open", self.chunk_id("falcon harbor"), "--json")[1])
        self.assertEqual((target["how"], os.path.abspath(target["path"])), ("original", os.path.abspath(self.pdf)))

    def test_a_named_copy_that_was_tampered_with_is_replaced_not_reused(self):
        self.add_and_commit()
        first = json.loads(self.run_cli("open", "falcon.pdf", "--page", "1", "--json")[1])["path"]
        with open(first, "wb") as f:
            f.write(b"not the pdf")
        again = json.loads(self.run_cli("open", "falcon.pdf", "--page", "1", "--json")[1])["path"]
        with open(again, "rb") as f:
            self.assertEqual(f.read(), TWO_PAGES)

    def test_the_command_line_makes_pdf_readable_by_itself(self):
        conv_registry.CONVERTERS[:] = [c for c in conv_registry.CONVERTERS if c.name != "pdf"]     # undo what the fixture did
        self.add_and_commit()
        self.assertIn("falcon.pdf", self.run_cli("search", "quantization sensitivity", "--mode", "lexical")[1])

    def test_a_source_can_be_opened_at_a_page_by_name(self):
        self.add_and_commit()
        target = json.loads(self.run_cli("open", "falcon.pdf", "--page", "1", "--json")[1])
        self.assertEqual(target["page"], 1)
        self.assertTrue(target["url"].endswith("#page=1"))

    def test_a_page_that_does_not_exist_is_refused(self):
        self.add_and_commit()
        code, _, err = self.run_cli("open", "falcon.pdf", "--page", "3")
        self.assertEqual(code, 2)
        self.assertIn("2 pages", err)
        self.assertEqual(self.run_cli("open", "falcon.pdf", "--page", "0")[0], 2)

    def test_a_kept_original_that_changed_is_reported_and_not_opened(self):
        self.add_and_commit("--ref")
        with open(self.pdf, "wb") as f:
            f.write(make_pdf([[block(80, "a different file now")]]))
        code, out, err = self.run_cli("open", self.chunk_id("falcon harbor"))
        self.assertEqual(code, 3)
        self.assertIn("source_changed", out)
        self.assertIn("not opened", err)
        self.assertNotIn("url:  file", out)

    def test_a_kept_original_that_vanished_is_reported_and_not_opened(self):
        self.add_and_commit("--ref")
        os.remove(self.pdf)
        code, out, err = self.run_cli("open", self.chunk_id("falcon harbor"))
        self.assertEqual(code, 3)
        self.assertIn("source_unavailable", out)

    def test_launch_hands_the_page_url_to_the_default_viewer_and_only_when_asked(self):
        self.add_and_commit()
        cid = self.chunk_id("quantization sensitivity")
        with mock.patch("webbrowser.open") as opened:
            self.run_cli("open", cid)
            opened.assert_not_called()
            self.run_cli("open", cid, "--launch")
        opened.assert_called_once()
        self.assertTrue(opened.call_args[0][0].endswith("falcon.pdf#page=2"))

    def test_nothing_is_launched_for_a_file_that_is_not_the_indexed_one(self):
        self.add_and_commit("--ref")
        os.remove(self.pdf)
        with mock.patch("webbrowser.open") as opened:
            self.run_cli("open", self.chunk_id("falcon harbor"), "--launch")
        opened.assert_not_called()

    def test_a_source_hidden_at_level_b_cannot_be_opened_by_name(self):
        self.add_and_commit()
        self.assertEqual(self.run_cli("delete", "falcon.pdf", "--level", "b", "--by", "roy", "--yes")[0], 0)
        self.run_cli("commit")
        code, _, err = self.run_cli("open", "falcon.pdf", "--page", "1")
        self.assertEqual(code, 2)
        self.assertIn("matches 0 sources", err)

    def test_the_search_result_line_names_the_page(self):
        self.add_and_commit()
        out = self.run_cli("search", "quantization sensitivity", "--chunks", "--mode", "lexical")[1]
        self.assertRegex(out, r"falcon\.pdf\)\s+page 2")


class TestEmbeddingCommands(Cli):
    def setUp(self):
        super().setUp()
        self.run_cli("init")
        self.write("cars.md", b"# Cars\n\nThe automobile repair manual covers brakes and engines.\n")
        self.write("tax.md", b"# Tax\n\nThe filing deadline for the annual return is in April.\n")
        self.run_cli("add", self.src, "--settle", "0")
        self.model = os.path.join(self.d, "model.gguf")
        with open(self.model, "wb") as f:
            f.write(b"weights")
        self.stub = Stub()
        self.addCleanup(self.stub.close)

    def pin(self, model=None):
        code, out, _ = self.run_cli("embedder", "set", "--server", os.sys.executable, "--model", model or self.model)
        self.assertEqual(code, 0, out)
        e = embedder.LlamaServerEmbedder(Home(self.home_path))
        os.makedirs(os.path.dirname(e.state_path), exist_ok=True)
        with open(e.state_path, "w", encoding="utf-8") as f:                            # the "resident server" is the stub
            json.dump({"pid": os.getpid(), "port": self.stub.port, "fingerprint": e._fingerprint()}, f)
        return out

    def test_pinning_prints_what_was_accepted(self):
        out = self.pin()
        self.assertIn("model.gguf", out)
        self.assertRegex(out, r"sha256 [0-9a-f]{16}")
        status = self.run_cli("embedder", "status")[1]
        self.assertIn("running: True", status)

    def test_without_a_backend_commit_is_lexical_and_says_nothing_extra(self):
        code, out, _ = self.run_cli("commit")
        self.assertEqual(code, 0)
        self.assertNotIn("vectors:", out)
        self.assertEqual(self.run_cli("embed")[0], 2)
        code, _, err = self.run_cli("search", "deadline", "--mode", "hybrid")
        self.assertEqual(code, 2)
        self.assertIn("needs an embedding backend", err)
        code, out, err = self.run_cli("search", "deadline")                              # auto, no backend: plain lexical, no notice
        self.assertEqual((code, err), (0, ""))
        self.assertIn("tax.md", out)

    def test_embed_then_commit_gives_a_commit_with_vectors_and_a_hybrid_search(self):
        self.pin()
        code, out, _ = self.run_cli("embed")
        self.assertEqual(code, 0, out)
        self.assertIn("every chunk has its vector", out)
        code, out, _ = self.run_cli("commit")
        self.assertEqual(code, 0)
        self.assertIn("vectors: model.gguf, 4 dimensions", out)
        self.assertEqual(len(self.stub.requests) > 0, True)
        code, out, err = self.run_cli("search", "deadline", "--mode", "hybrid")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("tax.md", out)

    def test_a_vector_file_damaged_without_changing_its_size_is_found_by_doctor_deep_not_by_the_quick_check(self):
        """Serving checks sizes only (hashing the files at every query would cost more than the query); the hashes are checked on demand."""
        from kb import commit, db
        self.pin()
        self.run_cli("embed")
        self.assertEqual(self.run_cli("commit")[0], 0)
        conn = db.connect(Home(self.home_path).db_path, readonly=True)
        try:
            path = os.path.join(commit.segment_dir(Home(self.home_path), commit.active_commit_id(conn)), "vec.npy")
        finally:
            conn.close()
        with open(path, "rb") as f:
            data = bytearray(f.read())
        data[-5] ^= 0xFF
        with open(path, "wb") as f:
            f.write(bytes(data))
        self.assertEqual(self.run_cli("doctor")[0], 0)
        code, out, _ = self.run_cli("doctor", "--deep")
        self.assertEqual(code, 1)
        self.assertIn("vec.npy", out)

    def test_a_second_embed_finds_nothing_left_to_do(self):
        self.pin()
        self.run_cli("embed")
        calls = len(self.stub.requests)
        out = self.run_cli("embed")[1]
        self.assertIn("0 without a vector", out)
        self.assertEqual(len(self.stub.requests), calls)

    def test_embed_with_a_limit_embeds_only_that_many_and_says_what_is_left(self):
        self.pin()
        out = self.run_cli("embed", "--limit", "1")[1]
        self.assertIn("embedding 1", out)
        self.assertRegex(out, r"\d+ still without a vector")
        self.assertIn("every chunk has its vector", self.run_cli("embed")[1])

    def test_a_commit_that_needs_many_vectors_refuses_and_names_the_command(self):
        self.pin()
        with mock.patch("kb.vecindex.VectorExtension.__init__", lambda self_, home, emb, max_inline=0: setattr(self_, "home", home) or
                        setattr(self_, "embedder", emb) or setattr(self_, "max_inline", 0) or setattr(self_, "_identity", None)):
            code, _, err = self.run_cli("commit")
        self.assertEqual(code, 2)
        self.assertIn("kb embed", err)

    def test_no_vectors_gives_a_lexical_only_commit_even_with_a_backend(self):
        self.pin()
        code, out, _ = self.run_cli("commit", "--no-vectors")
        self.assertEqual(code, 0)
        self.assertNotIn("vectors:", out)
        code, out, err = self.run_cli("search", "deadline")
        self.assertEqual(code, 0)
        self.assertIn("no vector index", err)                                            # the fallback is visible
        self.assertIn("tax.md", out)

    def test_after_the_model_is_replaced_the_old_vectors_are_refused_and_the_fallback_says_why(self):
        self.pin()
        self.run_cli("embed")
        self.run_cli("commit")
        with open(self.model, "wb") as f:
            f.write(b"another model entirely")
        self.pin()                                                                       # approve the new file
        code, out, err = self.run_cli("search", "deadline")
        self.assertEqual(code, 0)
        self.assertIn("another identity", err)
        self.assertIn("model_sha256", err)
        self.assertIn("tax.md", out)
        code, _, err = self.run_cli("search", "deadline", "--mode", "hybrid")
        self.assertEqual(code, 2)
        self.assertIn("another identity", err)
        self.assertEqual(self.run_cli("embed")[0], 0)                                    # the way back: embed again, commit again
        self.assertEqual(self.run_cli("commit")[0], 0)
        code, out, err = self.run_cli("search", "deadline", "--mode", "hybrid")
        self.assertEqual((code, err), (0, ""))

    def test_a_stopped_backend_status_and_stop(self):
        self.pin()
        with mock.patch.object(embedder.LlamaServerEmbedder, "_kill") as kill:
            self.assertIn("stopped", self.run_cli("embedder", "stop")[1])
            kill.assert_called_once()
        self.assertIn("running: False", self.run_cli("embedder", "status")[1])
        self.assertIn("not running", self.run_cli("embedder", "stop")[1])

    def test_the_state_file_and_the_configuration_hold_no_secrets_and_no_text(self):
        self.pin()
        self.run_cli("embed")
        cfg = read_json(embedder.config_path(Home(self.home_path)))
        self.assertEqual(sorted(cfg), ["dim", "instruction", "model", "model_id", "model_sha256", "server", "verified"])
        self.assertIsNone(re.search(r"automobile|deadline", json.dumps(cfg)))


class TestOpenedCopies(Cli):
    """`kb open` leaves a named copy of the original; a copy of something that was hidden must not stay behind (T-D3, Gate 1 H2)."""

    def setUp(self):
        super().setUp()
        self.run_cli("init")
        self.write("keep.pdf", TWO_PAGES)
        self.write("secret.md", b"# Secret\n\nThe zqcanaryopened phrase lives only here.\n")
        self.run_cli("add", self.src, "--settle", "0")
        self.run_cli("commit")

    def opened(self):
        root = os.path.join(self.home_path, "opened")
        return sorted(os.path.join(d, n) for d, _, names in os.walk(root) for n in names) if os.path.isdir(root) else []

    def test_a_copy_of_a_hidden_source_is_removed_at_the_next_commit_and_a_visible_one_stays(self):
        self.run_cli("open", "keep.pdf", "--page", "1")
        self.run_cli("open", "secret.md")
        self.assertEqual(sorted(os.path.basename(p) for p in self.opened()), ["keep.pdf", "secret.md"])
        self.run_cli("delete", "secret.md", "--level", "b", "--by", "roy", "--yes")
        self.assertEqual(len(self.opened()), 2)                                  # hiding takes effect at the next commit, like everything else
        self.run_cli("commit")
        self.assertEqual([os.path.basename(p) for p in self.opened()], ["keep.pdf"])

    def test_a_commit_cut_off_after_the_activation_is_finished_by_the_next_commit_even_when_nothing_changed(self):
        from kb import commit
        self.run_cli("open", "secret.md")
        self.run_cli("delete", "secret.md", "--level", "b", "--by", "roy", "--yes")
        with mock.patch.object(commit, "purge_opened"):                              # the process dies right after the activation
            code, out, _ = self.run_cli("commit")
        self.assertEqual(code, 5)                                                    # published, and it says that the erasure is not finished
        self.assertIn("WARNING", out)
        self.assertIn("opened copy", out)
        self.assertEqual([os.path.basename(p) for p in self.opened()], ["secret.md"])
        code, out, _ = self.run_cli("commit")
        self.assertEqual(code, 0)
        self.assertIn("unchanged", out)
        self.assertEqual(self.opened(), [])

    def test_a_chunk_id_is_no_way_around_a_hidden_source(self):
        """Knowing an id is not an access right: the chunk path must refuse what the name path refuses, before and after the next commit."""
        cid = self.chunk_id("zqcanaryopened")
        self.run_cli("delete", "secret.md", "--level", "b", "--by", "roy", "--yes")
        code, out, err = self.run_cli("open", cid)                                  # still in the active commit, but hidden from general access
        self.assertNotEqual(code, 0)
        self.assertEqual(self.opened(), [])
        self.run_cli("commit")
        code, out, err = self.run_cli("open", cid)                                  # now also not in the active commit
        self.assertNotEqual(code, 0)
        self.assertIn("not part of the active commit", err)
        self.assertEqual(self.opened(), [])

    def test_a_chunk_id_that_the_active_commit_does_not_hold_opens_nothing(self):
        code, out, err = self.run_cli("open", "ck_" + "a" * 32)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.opened(), [])

    def test_a_chunk_of_an_older_commit_is_not_opened_once_its_text_has_changed(self):
        old = self.chunk_id("zqcanaryopened")
        self.write("secret.md", b"# Secret\n\nThe zqcanaryopened phrase was rewritten into something else entirely.\n")
        self.run_cli("add", self.src, "--settle", "0")
        self.assertEqual(self.run_cli("commit")[0], 0)
        self.assertNotEqual(old, self.chunk_id("zqcanaryopened"))
        code, out, err = self.run_cli("open", old)
        self.assertNotEqual(code, 0)
        self.assertIn("not part of the active commit", err)

    def test_after_hiding_the_canary_is_in_the_stored_original_and_nowhere_else_that_holds_files(self):
        from kb import db, inventory
        from kb.home import Home
        self.run_cli("open", "secret.md")
        self.run_cli("delete", "secret.md", "--level", "b", "--by", "roy", "--yes")
        self.run_cli("commit")
        home = Home(self.home_path)
        conn = db.connect(home.db_path, readonly=True)
        try:
            found = inventory.scan(home, conn, "zqcanaryopened")
        finally:
            conn.close()
        self.assertTrue(found["raw_blobs"])                                      # kept on purpose (Gate 1 decision 4)
        for name in ("opened_copies", "vector_cache", "embedder_state", "logs", "index_segments", "staging"):
            self.assertFalse(found[name], name)

    def test_a_copy_with_no_matching_source_is_removed_too(self):
        os.makedirs(os.path.join(self.home_path, "opened", "deadbeefdeadbeef"))
        with open(os.path.join(self.home_path, "opened", "deadbeefdeadbeef", "x.pdf"), "wb") as f:
            f.write(b"orphan")
        self.write("more.md", b"# More\n\nSomething new to force a commit.\n")
        self.run_cli("add", self.src, "--settle", "0")
        self.run_cli("commit")
        self.assertEqual(self.opened(), [])


class TestDoctorEmbedding(Cli):
    def test_the_doctor_reports_the_pinned_backend_or_says_there_is_none(self):
        self.run_cli("init")
        caps = {c["name"]: c for c in json.loads(self.run_cli("doctor", "--json")[1])["capabilities"]}
        self.assertFalse(caps["embedding_backend"]["available"])
        self.assertIn("kb embedder set", caps["embedding_backend"]["detail"])
        model = os.path.join(self.d, "m.gguf")
        with open(model, "wb") as f:
            f.write(b"w")
        self.run_cli("embedder", "set", "--server", os.sys.executable, "--model", model)
        caps = {c["name"]: c for c in json.loads(self.run_cli("doctor", "--json")[1])["capabilities"]}
        self.assertTrue(caps["embedding_backend"]["available"])
        os.remove(model)
        caps = {c["name"]: c for c in json.loads(self.run_cli("doctor", "--json")[1])["capabilities"]}
        self.assertFalse(caps["embedding_backend"]["available"])
        self.assertIn("model file not found", caps["embedding_backend"]["detail"])



if __name__ == "__main__":
    unittest.main()
