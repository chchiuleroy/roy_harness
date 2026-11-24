"""Folder scanning that stays inside the folder it was given (Gate 1 H1, Gate 2 F28, T-WIN1..3).

Never followed: junctions and symbolic links (any file-system reparse point), shortcuts (.lnk, .url), cloud placeholders that are not
downloaded (reading them would trigger a download). Refused names: reserved device names (CON, NUL, COM1 ...), trailing space or dot,
names carrying a colon (alternate data streams) or characters Windows forbids. Case-only collisions inside one folder are reported,
not merged. Over-long paths are opened through the extended-length prefix. Every refusal is returned with a reason so it can be shown.
"""
import os
import stat
from dataclasses import dataclass
from typing import Optional

RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
BAD_CHARS = set('<>"|?*') | {chr(c) for c in range(32)}
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
PLACEHOLDER_MASK = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
LONG_PATH_LIMIT = 240
SHORTCUT_SUFFIXES = (".lnk", ".url")
EXTENDED = chr(92) * 2 + "?" + chr(92)


@dataclass(frozen=True)
class Entry:
    path: str                         # absolute path, forward slashes
    rel: str                          # relative to the scanned root
    skip: Optional[str] = None        # reason, or None when the file may be read


def long_path(path):
    """Windows: paths of 240+ characters need the extended-length prefix to be opened at all."""
    p = os.path.abspath(path)
    if os.name == "nt" and len(p) >= LONG_PATH_LIMIT and not p.startswith(EXTENDED):
        if p.startswith(chr(92) * 2):
            return EXTENDED + "UNC" + p[1:]
        return EXTENDED + p
    return p


def name_problem(name):
    stem = name.split(".")[0].rstrip(" ").lower()
    if stem in RESERVED:
        return "reserved_device_name"
    if ":" in name:
        return "colon_in_name_possible_alternate_data_stream"
    if any(c in BAD_CHARS for c in name):
        return "invalid_character_in_name"
    if name.endswith((" ", ".")):
        return "trailing_space_or_dot"
    return None


def attributes(path):
    try:
        return getattr(os.lstat(long_path(path)), "st_file_attributes", 0)
    except OSError:
        return 0


def is_reparse_point(path):
    try:
        st = os.lstat(long_path(path))
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def is_cloud_placeholder(path):
    return bool(attributes(path) & PLACEHOLDER_MASK)


def find_case_collisions(names):
    """Names that differ only by case. Returns the names that must be skipped (every one after the first in sorted order)."""
    seen, skipped = {}, {}
    for n in sorted(names):
        key = n.casefold()
        if key in seen:
            skipped[n] = seen[key]
        else:
            seen[key] = n
    return skipped


def _norm(path):
    return os.path.abspath(path).replace(os.sep, "/")


def _real(path):
    """realpath without the extended-length prefix, so that long paths compare equal to short ones."""
    p = os.path.realpath(long_path(path))
    if p.startswith(EXTENDED):
        p = p[len(EXTENDED):]
        if p.startswith("UNC" + chr(92)):
            p = chr(92) + p[3:]
    return p


def entry_problem(path, name, is_dir):
    if is_reparse_point(path):
        return "reparse_point_not_followed"
    if is_cloud_placeholder(path):
        return "cloud_placeholder_not_downloaded"
    p = name_problem(name)
    if p:
        return p
    if not is_dir and name.lower().endswith(SHORTCUT_SUFFIXES):
        return "shortcut_not_followed"
    return None


def scan_folder(root):
    """Yield an Entry for every file under root (and for every directory or file that was refused). Deterministic order."""
    root_abs = os.path.abspath(root)
    real_root = _real(root_abs)
    stack = [root_abs]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(long_path(d)) as it:
                listing = sorted(it, key=lambda e: e.name)
        except OSError as e:
            yield Entry(_norm(d), _rel(root_abs, d), f"directory_unreadable:{type(e).__name__}")
            continue
        collisions = find_case_collisions([e.name for e in listing])
        subdirs = []
        for e in listing:
            full = os.path.join(d, e.name)
            rel = _rel(root_abs, full)
            if e.name in collisions:
                yield Entry(_norm(full), rel, f"case_collision_with:{collisions[e.name]}")
                continue
            is_dir = e.is_dir(follow_symlinks=False)
            problem = entry_problem(full, e.name, is_dir)
            if problem:
                yield Entry(_norm(full), rel, problem)
                continue
            try:
                inside = os.path.commonpath([real_root, _real(full)]) == real_root
            except ValueError:
                inside = False
            if not inside:
                yield Entry(_norm(full), rel, "outside_the_folder")
                continue
            if is_dir:
                subdirs.append(full)
            elif e.is_file(follow_symlinks=False):
                yield Entry(_norm(full), rel, None)
        stack.extend(reversed(subdirs))


def is_inside(root, path):
    """True if the resolved location of path is inside the resolved root."""
    try:
        return os.path.commonpath([_real(root), _real(path)]) == _real(root)
    except ValueError:
        return False


def _rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/")
