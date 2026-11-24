"""S1f: the command line end to end (add, process, commit, search, status, inventory, links, rollback, recover) and the health checks that
guard the commit (doctor)."""
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stderr

from kb import cli, commit, doctor
from kb.home import Home

from .support import SRC, tmpdir


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir(self)
        self.home = os.path.join(self.d, "h")
        self.src = os.path.join(self.d, "src")
        os.makedirs(self.src)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(["--home", self.home, *argv], out=out)
        return code, out.getvalue(), err.getvalue()

    def write(self, name, data):
        p = os.path.join(self.src, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
        return p

    def fill(self):
        self.write("notes/alpha.md", "# Alpha\n\nThe falcon flies over the harbor at dawn. See [[beta]].\n")
        self.write("notes/beta.md", "---\ntitle: Beta Notes\n---\nBack to [[alpha]]; kelp forests grow offshore.\n")
        self.write("data.csv", "name,city\nAnn,Taipei\n")
        self.write("pic.png", b"\x89PNG....")
        self.write("short.lnk", "")


class TestFlow(Cli):
    def test_add_commit_search_status_roundtrip(self):
        self.assertEqual(self.run_cli("init")[0], 0)
        self.fill()
        code, out, _ = self.run_cli("add", self.src, "--settle", "0")
        self.assertEqual(code, 0)
        self.assertIn("skipped", out)
        self.assertIn("candidates until the next `kb commit`", out)
        code, out, _ = self.run_cli("status")
        self.assertIn("awaiting_conversion: 4", out)
        self.assertIn("short.lnk: shortcut_not_followed", out)
        self.assertIn("active_commit: none", out)
        code, out, _ = self.run_cli("commit")
        self.assertEqual(code, 0)
        self.assertIn("converted 4 revision(s): ok 3, partial 0, failed 1", out)
        self.assertIn("relations: 2 resolved", out)
        self.assertIn("created", out)
        code, out, _ = self.run_cli("status")
        self.assertIn("pic.png: unsupported_type", out)
        self.assertRegex(out, r"active_commit: cm_\w+")
        code, out, _ = self.run_cli("search", "falcon harbor")
        self.assertEqual(code, 0)
        self.assertIn("Alpha  (alpha.md)", out)
        self.assertIn("matched: falcon, harbor", out)
        code, out, _ = self.run_cli("search", "kelp", "--json")
        hits = json.loads(out)
        self.assertEqual([(h["name"], h["state"], h["status"]) for h in hits], [("beta.md", "committed", "ok")])
        self.assertEqual(self.run_cli("search", "nothing-matches-this")[1].strip(), "no results")
        code, out, _ = self.run_cli("links", "alpha.md")
        self.assertIn("alpha.md links to 1:", out)
        self.assertIn("beta.md", out)
        self.assertEqual(self.run_cli("commit")[1].splitlines()[-1].split(": ")[1].split(" ")[0], "unchanged")

    def test_candidates_and_rollback(self):
        self.run_cli("init")
        self.fill()
        self.run_cli("add", self.src, "--settle", "0")
        self.run_cli("commit")
        self.write("notes/fresh.md", "# Fresh\n\nmeteorology notes\n")
        self.run_cli("add", self.src, "--settle", "0")
        code, out, _ = self.run_cli("search", "meteorology")
        self.assertIn("no results", out)
        code, out, _ = self.run_cli("search", "meteorology", "--candidates")
        self.assertIn("(unprocessed candidate)", out)
        self.run_cli("commit")
        self.assertNotIn("candidate", self.run_cli("search", "meteorology", "--candidates")[1])
        code, out, _ = self.run_cli("rollback")
        self.assertEqual(code, 0)
        self.assertIn("no results", self.run_cli("search", "meteorology")[1])          # back on the commit that did not know the new file
        self.assertIn("(unprocessed candidate)", self.run_cli("search", "meteorology", "--candidates")[1])

    def test_search_before_any_commit_says_so(self):
        self.run_cli("init")
        code, out, err = self.run_cli("search", "anything")
        self.assertEqual(code, 3)
        self.assertIn("no active commit", err)

    def test_pasted_text_and_url(self):
        self.run_cli("init")
        self.run_cli("add", "--text", "# Pasted\n\ncontent about geysers", "--name", "paste.md")

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = b"<html><title>Web</title><p>an article about aurora borealis</p><script>CANARY</script></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_address[1]}/article"
        code, out, err = self.run_cli("add", "--url", url)
        self.assertIn("skipped", out)                                                    # loopback is refused without the flag
        self.assertEqual(self.run_cli("add", "--url", url, "--allow-private")[0], 0)
        self.run_cli("commit")
        self.assertIn("paste.md", self.run_cli("search", "geysers")[1])
        self.assertIn("article.html", self.run_cli("search", "aurora")[1])
        self.assertIn("no results", self.run_cli("search", "CANARY")[1])                 # script text never reached the index

    def test_inventory_lists_every_class(self):
        self.run_cli("init")
        self.fill()
        self.run_cli("add", self.src, "--settle", "0")
        self.run_cli("commit")
        out = self.run_cli("inventory")[1]
        for cls in ("raw_blobs", "index_segments", "staging", "backups", "logs", "database"):
            self.assertRegex(out, rf"(?m)^\s*{cls}\s")                  # the class name itself, not a folder name that happens to contain it
        self.assertRegex(out, r"raw_blobs\s+\d+ files")

    def test_a_second_writer_is_refused(self):
        self.run_cli("init")
        env = dict(os.environ, PYTHONPATH=SRC, PYTHONUTF8="1")
        code = ("import sys,time;from kb.writer import WriterLock;l=WriterLock(sys.argv[1]).acquire();print('locked',flush=True);time.sleep(60)")
        p = subprocess.Popen([sys.executable, "-c", code, self.home], stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.kill)
        self.assertEqual(p.stdout.readline().strip(), "locked")
        code, out, err = self.run_cli("commit")
        self.assertEqual(code, 2)
        self.assertIn("another process holds the kb writer lock", err)
        self.assertEqual(self.run_cli("status")[0], 0)                                    # reading is never blocked

    def test_a_crashed_commit_is_finished_by_the_next_writer_command(self):
        self.run_cli("init")
        self.fill()
        self.run_cli("add", self.src, "--settle", "0")
        env = dict(os.environ, PYTHONPATH=SRC, PYTHONUTF8="1", KB_FAULT="commit.after_journal")
        r = subprocess.run([sys.executable, "-m", "kb", "--home", self.home, "commit"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 137)
        code, out, _ = self.run_cli("process")
        self.assertIn("recovered commit", out)
        self.assertIn("completed", out)
        self.assertIn("Alpha", self.run_cli("search", "falcon")[1])

    def test_module_entry_point(self):
        env = dict(os.environ, PYTHONPATH=SRC, PYTHONUTF8="1")
        r = subprocess.run([sys.executable, "-m", "kb", "--home", self.home, "init"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([sys.executable, "-m", "kb", "--home", self.home, "status"], capture_output=True, text=True, env=env)
        self.assertIn("sources: 0", r.stdout)


class TestDoctorCommitChecks(Cli):
    def committed(self):
        self.run_cli("init")
        self.fill()
        self.run_cli("add", self.src, "--settle", "0")
        self.run_cli("commit")
        self.h = Home(self.home)

    def test_a_healthy_committed_home_passes_even_the_deep_check(self):
        self.committed()
        code, out, _ = self.run_cli("doctor", "--deep")
        self.assertEqual(code, 0, out)

    def test_missing_active_files_are_a_problem_with_a_next_step(self):
        self.committed()
        for d in os.listdir(self.h.sub("segments")):
            shutil.rmtree(os.path.join(self.h.sub("segments"), d))
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("active_commit_artifacts", out)
        self.assertIn("rollback", out)

    def test_deep_check_finds_bit_rot_that_the_cheap_check_cannot(self):
        self.committed()
        seg = os.path.join(self.h.sub("segments"), os.listdir(self.h.sub("segments"))[0], commit.FTS)
        with open(seg, "r+b") as f:
            f.seek(3000)
            b = f.read(1)
            f.seek(3000)
            f.write(bytes([b[0] ^ 1]))
        self.assertEqual(self.run_cli("doctor")[0], 0)
        code, out, _ = self.run_cli("doctor", "--deep")
        self.assertEqual(code, 1)
        self.assertIn("active_commit_damaged", out)

    def test_leftover_staging_folders_are_reported_and_recover_removes_them(self):
        self.committed()
        os.makedirs(os.path.join(self.h.sub("staging"), "cm_leftoverleftoverleftov"))
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("leftover_staging", out)
        self.run_cli("recover")
        self.assertEqual(self.run_cli("doctor")[0], 0)


if __name__ == "__main__":
    unittest.main()
