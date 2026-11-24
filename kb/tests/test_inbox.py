"""T-IN1 / T-IN2 and the identity rules of intake (WC11-WC13, WC24, WC26): everything is recorded, nothing is silently lost,
a half-written or locked file is never taken, and a killed ingest resumes without duplicates."""
import contextlib
import hashlib
import http.server
import os
import threading
import time
import unittest

from kb import cas, colls, fetch, inbox, pathsafe
from kb.home import Home

from .support import DbTestCase, run_py, tmpdir

NT = os.name == "nt"


class Base(DbTestCase):
    def setUp(self):
        super().setUp()
        self.d = tmpdir(self)
        self.home = Home(os.path.join(self.d, "h"))
        self.home.init()
        from kb import db
        self.conn = db.connect(self.home.db_path)
        self.cid = colls.create(self.conn, "docs")
        self.store = cas.BlobStore(self.home.sub("blobs"))
        self.intake = inbox.Intake(self.conn, self.store, self.cid, settle_s=0)
        self.src = os.path.join(self.d, "src")
        os.makedirs(self.src)

    def write(self, rel, data=b"hello", sub=None):
        p = os.path.join(self.src, *(sub or []), rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def count(self, table):
        return self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def skips(self):
        return {os.path.basename(p): r for p, r in self.conn.execute("SELECT path, reason FROM intake_skips")}


class TestFiles(Base):
    def test_a_new_file_is_recorded_with_everything_needed_to_trace_it(self):
        p = self.write("note.md", b"# Title\n\nbody")
        r = self.intake.add_file(p)
        self.assertEqual(r.outcome, "new")
        sha = hashlib.sha256(b"# Title\n\nbody").hexdigest()
        self.assertEqual(self.conn.execute("SELECT blob_sha256 FROM revisions WHERE revision_id=?", (r.revision_id,)).fetchone()[0], sha)
        kinds = dict(self.conn.execute("SELECT kind, value FROM source_locations WHERE source_id=?", (r.source_id,)))
        self.assertEqual(kinds["name"], "note.md")
        self.assertTrue(kinds["path"].endswith("/src/note.md"))
        self.assertEqual(self.store.verify(self.conn, sha), cas.OK)
        self.assertEqual(self.count("intake_skips"), 0)

    def test_same_location_same_bytes_is_unchanged_and_changed_bytes_are_a_new_revision(self):
        p = self.write("a.txt", b"one")
        first = self.intake.add_file(p)
        self.assertEqual(self.intake.add_file(p).outcome, "unchanged")
        self.write("a.txt", b"two")
        upd = self.intake.add_file(p)
        self.assertEqual((upd.outcome, upd.source_id), ("updated", first.source_id))
        self.assertNotEqual(upd.revision_id, first.revision_id)
        self.assertEqual(self.count("sources"), 1)
        self.assertEqual(self.count("revisions"), 2)               # the old revision is kept
        parent = self.conn.execute("SELECT parent_revision_id FROM revision_observations WHERE revision_id=?", (upd.revision_id,)).fetchone()[0]
        self.assertEqual(parent, first.revision_id)

    def test_going_back_to_an_earlier_content_returns_to_the_same_revision(self):
        """A, then B, then A again is the same revision A observed twice (content-addressed identity)."""
        p = self.write("a.txt", b"A")
        ra = self.intake.add_file(p)
        self.write("a.txt", b"B")
        self.intake.add_file(p)
        self.write("a.txt", b"A")
        back = self.intake.add_file(p)
        self.assertEqual(back.revision_id, ra.revision_id)
        self.assertEqual((self.count("revisions"), self.count("revision_observations")), (2, 3))

    def test_a_renamed_file_keeps_its_source(self):
        """WC12: same bytes, old path gone: the source is the same, a location is added, nothing is re-created."""
        p = self.write("old.txt", b"moving content")
        first = self.intake.add_file(p)
        new = os.path.join(self.src, "new name.txt")
        os.rename(p, new)
        r = self.intake.add_file(new)
        self.assertEqual((r.outcome, r.source_id), ("renamed", first.source_id))
        self.assertEqual(self.count("sources"), 1)
        paths = [v for (v,) in self.conn.execute("SELECT value FROM source_locations WHERE kind='path' ORDER BY seq")]
        self.assertTrue(paths[0].endswith("old.txt") and paths[1].endswith("new name.txt"))

    def test_identical_bytes_at_a_second_path_share_one_blob(self):
        """WC13: a copy is a new source (it can diverge later) but the evidence is stored once."""
        a = self.write("a.txt", b"same")
        b = self.write("copy.txt", b"same")
        ra, rb = self.intake.add_file(a), self.intake.add_file(b)
        self.assertEqual((ra.outcome, rb.outcome), ("new", "duplicate_blob"))
        self.assertNotEqual(ra.source_id, rb.source_id)
        self.assertEqual((self.count("sources"), self.count("blobs")), (2, 1))

    def test_refused_missing_and_special_files_are_recorded_with_reasons(self):
        self.assertEqual(self.intake.add_file(os.path.join(self.src, "nope.txt")).reason, "missing")
        self.assertEqual(self.intake.add_file(self.write("x.lnk")).reason, "shortcut_not_followed")
        self.assertEqual(self.intake.add_file(self.src).reason, "not_a_regular_file")
        self.assertEqual(self.skips(), {"nope.txt": "missing", "x.lnk": "shortcut_not_followed", "src": "not_a_regular_file"})

    def test_a_file_still_being_written_is_deferred_not_taken(self):
        """F30 / WC24: a half-written file would index as truncated content; it waits until it has been quiet for the settle time."""
        p = self.write("fresh.txt", b"just written")
        early = inbox.Intake(self.conn, self.store, self.cid, settle_s=3600)
        r = early.add_file(p)
        self.assertEqual((r.outcome, r.reason), ("skipped", "unstable_retry_later"))
        self.assertEqual(self.count("sources"), 0)
        self.assertEqual(self.skips(), {"fresh.txt": "unstable_retry_later"})
        later = self.intake.add_file(p)                           # settle time over: taken, and the skip row is cleared
        self.assertEqual(later.outcome, "new")
        self.assertEqual(self.count("intake_skips"), 0)

    @unittest.skipUnless(NT, "exclusive sharing is exercised through the Windows API")
    def test_a_file_locked_by_another_program_is_retried_later_not_failed(self):
        import ctypes
        p = self.write("locked.txt", b"in use")
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = ctypes.c_void_p
        handle = k32.CreateFileW(p, 0x80000000, 0, None, 3, 0, None)       # GENERIC_READ, share nothing
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            r = self.intake.add_file(p)
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
        self.assertEqual((r.outcome, r.reason), ("skipped", "temporarily_unreadable"))
        self.assertEqual(self.intake.add_file(p).outcome, "new")              # released: it is taken now

    def test_a_file_that_changes_while_it_is_read_is_not_recorded(self):
        p = self.write("racing.txt", b"before")
        real = self.store.put_path

        def racing(conn, path, mode="copy"):
            ref = real(conn, path, mode)
            time.sleep(0.02)
            with open(path, "ab") as f:
                f.write(b" and more")
            os.utime(path, ns=(1_000_000_000, 1_000_000_000))
            return ref
        self.store.put_path = racing
        r = self.intake.add_file(p)
        self.assertEqual((r.outcome, r.reason), ("skipped", "changed_while_reading"))
        self.assertEqual((self.count("sources"), self.count("revisions")), (0, 0))

    def test_reference_mode_records_without_copying(self):
        p = self.write("big.txt", b"by reference")
        r = self.intake.add_file(p, mode="ref")
        sha = hashlib.sha256(b"by reference").hexdigest()
        self.assertEqual(self.conn.execute("SELECT mode FROM blobs WHERE sha256=?", (sha,)).fetchone()[0], "ref")
        self.assertFalse(os.path.exists(self.store.blob_path(sha)))
        os.remove(p)
        self.assertEqual(self.store.verify(self.conn, sha), cas.MISSING)    # the evidence is honestly reported as unavailable
        self.assertEqual(r.outcome, "new")


class TestFolders(Base):
    def test_a_mixed_folder_records_every_entry_and_a_rescan_changes_nothing(self):
        """WC11: supported and unsupported files all get a record; refused entries get a visible reason; running it again is a no-op."""
        for i in range(5):
            self.write(f"n{i}.md", f"# n{i}".encode(), sub=["docs"])
        self.write("data.csv", b"a,b\n1,2", sub=["docs"])
        self.write("photo.png", b"\x89PNG....", sub=["docs", "img"])
        self.write("x.lnk", sub=["docs"])
        res = self.intake.add_folder(os.path.join(self.src, "docs"))
        outcomes = sorted(r.outcome for r in res)
        self.assertEqual(outcomes, ["new"] * 7 + ["skipped"])
        self.assertEqual(self.skips(), {"x.lnk": "shortcut_not_followed"})
        before = (self.count("sources"), self.count("revisions"), self.count("blobs"))
        again = self.intake.add_folder(os.path.join(self.src, "docs"))
        self.assertEqual(sorted(r.outcome for r in again), ["skipped"] + ["unchanged"] * 7)
        self.assertEqual((self.count("sources"), self.count("revisions"), self.count("blobs")), before)

    def test_a_large_folder_is_ingested_completely(self):
        """T-IN2 at a size that runs in seconds: every file recorded once, none lost, none duplicated."""
        for i in range(150):
            self.write(f"f{i:03d}.txt", f"document number {i}".encode(), sub=["bulk", f"d{i % 7}"])
        res = self.intake.add_folder(os.path.join(self.src, "bulk"))
        self.assertEqual((len(res), self.count("sources"), self.count("blobs")), (150, 150, 150))

    def test_a_killed_ingest_resumes_without_duplicates(self):
        """WC26: a process killed half way through a folder leaves complete records only; the rerun finishes the job."""
        for i in range(12):
            self.write(f"f{i:02d}.txt", f"content {i}".encode(), sub=["resume"])
        folder = os.path.join(self.src, "resume")
        code = (f"import os;from kb import inbox,cas,db,colls;from kb.home import Home;h=Home({self.home.path!r});"
                f"c=db.connect(h.db_path);cid={self.cid!r};i=inbox.Intake(c,cas.BlobStore(h.sub('blobs')),cid,settle_s=0);"
                f"files=sorted(os.listdir({folder!r}))[:5];[i.add_file(os.path.join({folder!r},f)) for f in files];os._exit(137)")
        self.assertEqual(run_py(code).returncode, 137)
        self.assertEqual(self.count("sources"), 5)
        res = self.intake.add_folder(folder)
        self.assertEqual(sorted(r.outcome for r in res), ["new"] * 7 + ["unchanged"] * 5)
        self.assertEqual((self.count("sources"), self.count("blobs")), (12, 12))


class TestTextAndUrl(Base):
    def test_pasted_text_is_a_source_named_by_content_or_by_the_user(self):
        r = self.intake.add_text("hello world")
        name = self.conn.execute("SELECT value FROM source_locations WHERE source_id=? AND kind='name'", (r.source_id,)).fetchone()[0]
        self.assertTrue(name.startswith("pasted-") and name.endswith(".txt"))
        self.assertEqual(self.intake.add_text("hello world").outcome, "unchanged")
        named = self.intake.add_text("v1", name="meeting.md")
        self.assertEqual(self.intake.add_text("v2", name="meeting.md").outcome, "updated")
        self.assertEqual(self.intake.add_text("v2", name="meeting.md").source_id, named.source_id)

    def test_url_snapshots_the_bytes_and_tracks_changes(self):
        pages = {"v": b"<html><title>T</title><p>first</p></html>"}
        calls = []

        def fake(url, allow_private=False, resolver=None):
            calls.append(url)
            return pages["v"], "text/html", url
        intake = inbox.Intake(self.conn, self.store, self.cid, settle_s=0, fetcher=fake)
        r = intake.add_url("https://example.com/docs/page")
        self.assertEqual(r.outcome, "new")
        name = self.conn.execute("SELECT value FROM source_locations WHERE source_id=? AND kind='name'", (r.source_id,)).fetchone()[0]
        self.assertEqual(name, "page.html")                              # a name without an extension gets one from the content type
        self.assertEqual(intake.add_url("https://example.com/docs/page").outcome, "unchanged")
        pages["v"] = b"<html><p>second</p></html>"
        self.assertEqual(intake.add_url("https://example.com/docs/page").outcome, "updated")
        self.assertEqual(self.store.verify(self.conn, hashlib.sha256(b"<html><title>T</title><p>first</p></html>").hexdigest()), cas.OK)

    def test_a_refused_url_is_recorded_not_raised(self):
        def refuse(url, allow_private=False, resolver=None):
            raise fetch.FetchRefused("nope")
        r = inbox.Intake(self.conn, self.store, self.cid, settle_s=0, fetcher=refuse).add_url("http://x.example/a")
        self.assertEqual((r.outcome, r.reason), ("skipped", "fetch_refused:nope"))
        self.assertEqual(self.count("sources"), 0)


class TestFetchRules(unittest.TestCase):
    PUBLIC = staticmethod(lambda host, port, type=0: [(2, 1, 6, "", ("93.184.216.34", port))])
    LOOPBACK = staticmethod(lambda host, port, type=0: [(2, 1, 6, "", ("127.0.0.1", port))])
    PRIVATE = staticmethod(lambda host, port, type=0: [(2, 1, 6, "", ("192.168.1.10", port))])

    def test_only_http_and_https(self):
        for bad in ("file:///etc/passwd", "ftp://x.example/a", "javascript:alert(1)", "gopher://x", "http://", "no scheme"):
            with self.assertRaises(fetch.FetchRefused, msg=bad):
                fetch.check_url(bad, resolver=self.PUBLIC)
        fetch.check_url("https://example.com/a", resolver=self.PUBLIC)

    def test_non_public_addresses_are_refused_unless_allowed(self):
        """A later agent-facing path must not be able to make kb request the local network."""
        for resolver in (self.LOOPBACK, self.PRIVATE):
            with self.assertRaises(fetch.FetchRefused):
                fetch.check_url("http://internal.example/", resolver=resolver)
            fetch.check_url("http://internal.example/", allow_private=True, resolver=resolver)

    def test_a_redirect_is_checked_like_the_original_request(self):
        handler = fetch._CheckedRedirects(False, self.PUBLIC)
        import urllib.request
        req = urllib.request.Request("https://example.com/a")
        for target in ("file:///etc/passwd", "ftp://x/y"):
            with self.assertRaises(fetch.FetchRefused):
                handler.redirect_request(req, None, 302, "Found", {}, target)
        handler = fetch._CheckedRedirects(False, self.LOOPBACK)
        with self.assertRaises(fetch.FetchRefused):
            handler.redirect_request(req, None, 302, "Found", {}, "http://localhost:8080/admin")

    def test_filename_hints(self):
        self.assertEqual(fetch.filename_hint("https://a.example/x/report.pdf", "application/pdf"), "report.pdf")
        self.assertEqual(fetch.filename_hint("https://a.example/", "text/html"), "index.html")
        self.assertEqual(fetch.filename_hint("https://a.example/notes", "text/markdown"), "notes.md")

    def test_real_http_roundtrip_size_cap_and_hostile_redirect(self):
        """A local server stands in for the web: the body arrives untouched, an oversize body is refused, a redirect to file:// is refused."""
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/ok":
                    body = b"<html><p>hello</p></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/big":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"x" * 5000)
                else:
                    self.send_response(302)
                    self.send_header("Location", "file:///etc/passwd")
                    self.end_headers()
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        data, ctype, final = fetch.fetch(base + "/ok", allow_private=True)
        self.assertEqual((data, ctype, final), (b"<html><p>hello</p></html>", "text/html", base + "/ok"))
        with self.assertRaises(fetch.FetchRefused):
            fetch.fetch(base + "/big", allow_private=True, max_bytes=1000)
        with self.assertRaises(fetch.FetchRefused):
            fetch.fetch(base + "/redirect", allow_private=True)
        with self.assertRaises(fetch.FetchRefused):
            fetch.fetch(base + "/ok")                                   # loopback is not public


if __name__ == "__main__":
    unittest.main()
