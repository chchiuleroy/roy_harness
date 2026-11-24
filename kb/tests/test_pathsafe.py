"""T-WIN1..3 and H1: a folder scan never leaves the folder, never follows links, and says why it refused something."""
import os
import shutil
import subprocess
import unittest

from kb import pathsafe
from kb.pathsafe import EXTENDED

from .support import tmpdir

NT = os.name == "nt"


def write(path, data=b"x"):
    with open(pathsafe.long_path(path), "wb") as f:
        f.write(data)


def make_link(link, target):
    """A directory link without administrator rights: a junction on Windows, a symlink elsewhere."""
    if NT:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True, text=True)
        if r.returncode != 0:
            raise unittest.SkipTest("cannot create a junction here: " + r.stderr.strip())
    else:
        os.symlink(target, link)


def by_rel(entries):
    return {e.rel: e.skip for e in entries}


class TestNames(unittest.TestCase):
    def test_reserved_and_forbidden_names(self):
        """These names behave specially on Windows (devices, data streams); they are refused with a reason, never opened."""
        for bad, why in [("CON", "reserved_device_name"), ("con.txt", "reserved_device_name"), ("Nul.md", "reserved_device_name"),
                         ("COM1", "reserved_device_name"), ("lpt9.log", "reserved_device_name"), ("a:b", "colon_in_name_possible_alternate_data_stream"),
                         ("x?y.txt", "invalid_character_in_name"), ("star*.md", "invalid_character_in_name"),
                         ("trail ", "trailing_space_or_dot"), ("dot.", "trailing_space_or_dot")]:
            self.assertEqual(pathsafe.name_problem(bad), why, bad)
        for ok in ("console.txt", "concat.md", "nulls.csv", "com10.txt", "readme.md", "報告.txt", "a b.txt", "x.y.z"):
            self.assertIsNone(pathsafe.name_problem(ok), ok)

    def test_case_collisions_keep_the_first_and_report_the_rest(self):
        self.assertEqual(pathsafe.find_case_collisions(["Readme.md", "README.md", "readme.md", "other.md"]),
                         {"Readme.md": "README.md", "readme.md": "README.md"})
        self.assertEqual(pathsafe.find_case_collisions(["a", "b"]), {})

    def test_long_paths_get_the_extended_prefix_only_when_needed(self):
        short, long_ = "C:/a/b.txt", "C:/" + "d/" * 130 + "f.txt"
        if NT:
            self.assertFalse(pathsafe.long_path(short).startswith(EXTENDED))
            self.assertTrue(pathsafe.long_path(long_).startswith(EXTENDED))
        else:
            self.assertEqual(pathsafe.long_path(short), os.path.abspath(short))


class TestScan(unittest.TestCase):
    def setUp(self):
        self.root = os.path.join(tmpdir(self), "root")
        os.makedirs(os.path.join(self.root, "sub", "deep"))
        write(os.path.join(self.root, "a.md"))
        write(os.path.join(self.root, "sub", "b.txt"))
        write(os.path.join(self.root, "sub", "deep", "c.csv"))

    def test_plain_tree_is_listed_in_a_stable_order(self):
        entries = list(pathsafe.scan_folder(self.root))
        self.assertEqual([e.rel for e in entries], ["a.md", "sub/b.txt", "sub/deep/c.csv"])
        self.assertTrue(all(e.skip is None for e in entries))
        self.assertEqual([e.rel for e in pathsafe.scan_folder(self.root)], [e.rel for e in entries])

    def test_a_link_to_a_folder_outside_is_not_followed(self):
        """H1: the boundary. Whatever is behind the link must not be indexed, and the refusal must be visible."""
        outside = os.path.join(tmpdir(self), "outside")
        os.makedirs(outside)
        write(os.path.join(outside, "secret.txt"), b"SECRET")
        make_link(os.path.join(self.root, "link"), outside)
        entries = list(pathsafe.scan_folder(self.root))
        skips = by_rel(entries)
        self.assertEqual(skips["link"], "reparse_point_not_followed")
        self.assertNotIn("link/secret.txt", skips)
        self.assertEqual(sorted(r for r, s in skips.items() if s is None), ["a.md", "sub/b.txt", "sub/deep/c.csv"])

    @unittest.skipUnless(NT, "needs Windows file attributes")
    def test_a_file_symlink_or_offline_placeholder_is_not_read(self):
        write(os.path.join(self.root, "target.txt"))
        link = os.path.join(self.root, "filelink.txt")
        try:
            os.symlink(os.path.join(self.root, "target.txt"), link)
        except OSError:
            self.skipTest("symlink creation needs a privilege here")
        self.assertEqual(by_rel(pathsafe.scan_folder(self.root))["filelink.txt"], "reparse_point_not_followed")

    def test_cloud_placeholder_attribute_is_refused_without_reading(self):
        """Reading a not-yet-downloaded cloud file would trigger a download; the attribute check must come first."""
        original = pathsafe.attributes
        pathsafe.attributes = lambda p: pathsafe.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS if p.endswith("b.txt") else 0
        self.addCleanup(setattr, pathsafe, "attributes", original)
        self.assertEqual(by_rel(pathsafe.scan_folder(self.root))["sub/b.txt"], "cloud_placeholder_not_downloaded")

    def test_shortcuts_are_not_followed(self):
        write(os.path.join(self.root, "link.lnk"))
        write(os.path.join(self.root, "site.url"))
        skips = by_rel(pathsafe.scan_folder(self.root))
        self.assertEqual((skips["link.lnk"], skips["site.url"]), ("shortcut_not_followed", "shortcut_not_followed"))

    @unittest.skipUnless(NT, "device names are a Windows rule")
    def test_reserved_device_names_are_listed_but_refused(self):
        """A file literally named nul.txt can exist (created with the extended prefix); opening it normally would hit the device."""
        p = os.path.join(self.root, "nul.txt")
        write(p)
        self.addCleanup(lambda: os.remove(pathsafe.long_path(p)))
        self.assertEqual(by_rel(pathsafe.scan_folder(self.root))["nul.txt"], "reserved_device_name")

    @unittest.skipUnless(NT, "alternate data streams are an NTFS feature")
    def test_an_alternate_data_stream_does_not_become_a_source(self):
        main = os.path.join(self.root, "main.txt")
        write(main, b"visible")
        try:
            with open(main + ":hidden", "wb") as f:
                f.write(b"HIDDEN STREAM")
        except OSError:
            self.skipTest("file system without alternate data streams")
        names = [e.rel for e in pathsafe.scan_folder(self.root)]
        self.assertIn("main.txt", names)
        self.assertEqual([n for n in names if ":" in n], [])

    def test_long_paths_are_scanned_and_readable(self):
        deep = self.root
        for _ in range(12):
            deep = os.path.join(deep, "d" * 25)
        os.makedirs(pathsafe.long_path(deep))
        target = os.path.join(deep, "leaf.txt")
        write(target, b"deep content")
        entries = [e for e in pathsafe.scan_folder(self.root) if e.rel.endswith("leaf.txt")]
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0].skip)
        self.assertGreater(len(entries[0].path), 260 if NT else 200)
        with open(pathsafe.long_path(entries[0].path), "rb") as f:
            self.assertEqual(f.read(), b"deep content")

    def test_an_unreadable_directory_is_reported_and_the_rest_continues(self):
        real = os.scandir

        def flaky(path):
            if os.path.basename(str(path).rstrip("/" + os.sep)) == "deep":
                raise PermissionError("denied")
            return real(path)
        os.scandir = flaky
        self.addCleanup(setattr, os, "scandir", real)
        skips = by_rel(pathsafe.scan_folder(self.root))
        self.assertEqual(skips["sub/deep"], "directory_unreadable:PermissionError")
        self.assertIsNone(skips["a.md"])
        self.assertIsNone(skips["sub/b.txt"])

    def test_a_path_that_resolves_outside_the_root_is_refused_even_if_no_link_was_detected(self):
        """Defence in depth for H1: the resolved location is checked, not only the link flags."""
        original = pathsafe._real
        pathsafe._real = lambda p: tmpdir(self) if p.endswith("b.txt") else original(p)
        self.addCleanup(setattr, pathsafe, "_real", original)
        self.assertEqual(by_rel(pathsafe.scan_folder(self.root))["sub/b.txt"], "outside_the_folder")


if __name__ == "__main__":
    unittest.main()
