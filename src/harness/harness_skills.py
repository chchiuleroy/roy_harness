"""Skills: instructions on demand (book ch 11).

A skill is a folder holding a SKILL.md: a name, a one-line description, and a body of
instructions. Only names and descriptions are shown to the model (the menu, in the
system prompt); the body enters the conversation when the model calls the `skill` tool
or the user types `/skill <name>`. That is progressive disclosure: keep a short index
in context, fetch the detail only when needed.

A skill body is text the model will OBEY, so skills get the trust of AGENT.md and the
hooks file: they come from local folders only, and the model cannot create or edit
them (otherwise it could rewrite its own rules and keep them across sessions).

SKILL.md is read with a small YAML-subset parser (plain, quoted, folded `>` and literal
`|` values, nested keys skipped). Only `name` and `description` are used; other keys
(`allowed-tools`, `metadata`, `argument-hint`, ...) are read and ignored, so no tool
restriction from them is enforced.
"""
import dataclasses
import json
import os
import re

import harness_hooks
import harness_mcp

MAX_SKILLS = 64             # my limits, not a spec's: the menu rides on every request
MAX_DESCRIPTION = 300
MIN_DESCRIPTION = 40         # over budget, descriptions shrink down to this, no further
MAX_MENU = 8_000
MAX_BODY = 20_000
MAX_FILE = 1_000_000        # bytes read from one SKILL.md
MAX_SCAN = 1_000            # folders examined in all roots together
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):[ \t]*(.*)$")
BLOCK_RE = re.compile(r"^[>|][+-]?\d*$")

SKILL_TOOL = {
    "name": "skill",
    "description": "Load the full instructions of one skill from the list in the system "
                   "prompt. Call it when the task matches a skill's description, then "
                   "follow the instructions it returns.",
    "input_schema": {"type": "object", "properties": {
        "name": {"type": "string", "description": "the skill's name from the list"}},
        "required": ["name"]}}


# --------------------------------------------------------------------- parser
def _unquote(s):
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            return json.loads(s)
        except ValueError:
            return s[1:-1]
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    return s


def _block(lines, i, style):
    """Collect an indented block scalar starting at lines[i]; -> (value, next index)."""
    block = []
    while i < len(lines) and (not lines[i].strip() or lines[i][:1] in " \t"):
        block.append(lines[i])
        i += 1
    while block and not block[-1].strip():
        block.pop()
    indents = [len(l) - len(l.lstrip(" \t")) for l in block if l.strip()]
    cut = min(indents) if indents else 0
    rows = [l[cut:] if l.strip() else "" for l in block]
    if style == "|":
        return "\n".join(rows), i
    out = ""
    for row in rows:                        # folded: lines join with a space, a blank line
        if not row:                         # becomes a line break
            out += "" if out.endswith("\n") else "\n"
        else:
            out += ("" if not out or out.endswith("\n") else " ") + row
    return out, i


def parse_frontmatter(text):
    """-> (flat dict of top-level keys, body). No or unterminated frontmatter -> ({}, text)."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text
    lines = text.split("\n")
    end = next((n for n in range(1, len(lines)) if lines[n].strip() in ("---", "...")), None)
    if end is None:
        return {}, text
    meta, i, head = {}, 0, lines[1:end]
    while i < len(head):
        m = KEY_RE.match(head[i])
        i += 1
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if BLOCK_RE.match(value):
            meta[key], i = _block(head, i, value[0])
            continue
        cont = []
        while i < len(head) and head[i][:1] in " \t" and head[i].strip():
            cont.append(head[i].strip())
            i += 1
        if not value:                       # nested map or list: skipped; plain multi-line joined
            nested = all(re.match(r"^(-\s|[^:\s][^:]*:)", c) for c in cont)
            meta[key] = "" if (nested or not cont) else " ".join(cont)
        else:
            meta[key] = _unquote(" ".join([value] + cont))
    return meta, "\n".join(lines[end + 1:])


# ------------------------------------------------------------------- registry
@dataclasses.dataclass
class Skill:
    name: str
    description: str
    folder: str                 # as found (may be a link)
    path: str                   # its SKILL.md


def _inside(real, roots):
    # a drive root ("C:\") already ends with a separator; adding another made every child
    # of it look outside (found by the review)
    return any(real == r or real.startswith(r if r.endswith(os.sep) else r + os.sep)
               for r in roots)


def _identity(path):
    """(device, file index) of an existing path: it survives a rename, a path does not."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino) if st.st_ino else None


def _read_text(path):
    """-> (text, None) or (None, reason)."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_FILE + 1)
    except OSError as e:
        return None, f"cannot read: {e}"
    if len(raw) > MAX_FILE:
        return None, f"file is larger than {MAX_FILE} bytes"
    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return None, "not valid UTF-8"


class Registry:
    def __init__(self):
        self.skills = {}            # name -> Skill (sorted at the end of load_all)
        self.warnings = []
        self.roots = []             # canonical real paths of every configured folder
        self.root_ids = set()       # identities of those that existed at start-up

    def is_empty(self):
        return not self.skills

    def menu(self):
        return "\n".join(f"- {s.name}: {s.description}"
                         for s in sorted(self.skills.values(), key=lambda s: s.name))

    def describe(self):
        return [f"{s.name}: {s.description[:70]}  ({s.folder})"
                for s in sorted(self.skills.values(), key=lambda s: s.name)]

    def load(self, name):
        """The skill's instructions as text for the model, or an ERROR string. Names are
        looked up in a table; they are never used as paths."""
        skill = self.skills.get(name) if isinstance(name, str) else None
        if skill is None:
            return (f"ERROR: unknown skill {name!r}; available: "
                    f"{', '.join(sorted(self.skills)) or '(none)'}")
        folder_real = harness_hooks.canonical(skill.folder)
        file_real = harness_hooks.canonical(skill.path)
        if not (_inside(folder_real, self.roots) and _inside(file_real, self.roots)):
            return (f"ERROR: skill {skill.name} now resolves outside the skills folders "
                    f"(its folder or SKILL.md was replaced by a link); refusing to load it")
        text, why = _read_text(file_real)            # open what was checked, not the link
        if text is None:
            return f"ERROR: skill {skill.name}: {why}"
        if harness_hooks.canonical(skill.path) != file_real:   # swapped while reading
            return f"ERROR: skill {skill.name} changed while it was being read; refusing it"
        body = parse_frontmatter(text)[1].strip("\n")
        if len(body) > MAX_BODY:
            body = (body[:MAX_BODY] + f"\n... [truncated {len(body) - MAX_BODY} more chars; "
                    f"read the full file with read_file: {skill.path}] ...")
        return f"[skill {skill.name}; its folder is {os.path.abspath(skill.folder)}]\n{body}"


EMPTY = Registry()


def load_all(dirs=None):
    """Scan the skills folders (default: HARNESS_SKILLS_DIRS, else ./skills). One broken
    skill is skipped with a warning; it never stops the others. Everything is validated
    first; the count and menu limits are then applied in global alphabetical order, so
    the order of the folders does not decide which skills survive."""
    if dirs is None:
        env = os.environ.get("HARNESS_SKILLS_DIRS")
        dirs = [d for d in env.split(os.pathsep) if d.strip()] if env else ["skills"]
    reg = Registry()
    reg.roots = [harness_hooks.canonical(d) for d in dirs]
    reg.root_ids = {i for i in (_identity(d) for d in dirs) if i}
    found, scanned = {}, 0
    for root in dirs:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            folder = os.path.join(root, name)
            md = os.path.join(folder, "SKILL.md")
            if not os.path.isdir(folder) or not os.path.isfile(md):
                continue
            scanned += 1
            if scanned > MAX_SCAN:
                reg.warnings.append(f"stopped scanning after {MAX_SCAN} folders")
                break
            if not _inside(harness_hooks.canonical(folder), reg.roots):
                reg.warnings.append(
                    f"skipped {name}: it is a link that leaves the skills folders (it points "
                    f"to {os.path.realpath(folder)}); add that folder to HARNESS_SKILLS_DIRS "
                    f"to use it")
                continue
            if not _inside(harness_hooks.canonical(md), reg.roots):
                reg.warnings.append(f"skipped {name}: its SKILL.md is a link that leaves the "
                                    f"skills folders (it points to {os.path.realpath(md)})")
                continue
            if not NAME_RE.match(name):
                reg.warnings.append(f"skipped {name!r}: the folder name must be 1-64 of "
                                    f"letters, digits, '.', '_' or '-', not starting with one "
                                    f"of the last three")
                continue
            text, why = _read_text(harness_hooks.canonical(md))
            if text is None:
                reg.warnings.append(f"skipped {name}: {why}")
                continue
            meta = parse_frontmatter(text)[0]
            description = re.sub(r"\s+", " ", meta.get("description", "")).strip()
            description = harness_mcp.clean_text(description, MAX_DESCRIPTION)
            if not description:
                reg.warnings.append(f"skipped {name}: SKILL.md has no description")
                continue
            if meta.get("name", "").strip() not in ("", name):
                reg.warnings.append(f"{name}: its name field says {meta['name'].strip()!r}; "
                                    f"the folder name {name!r} is used")
            if name in found:
                reg.warnings.append(f"skipped {name} from {root}: a skill with that name "
                                    f"was already loaded from {found[name].folder}")
                continue
            found[name] = Skill(name, description, folder, md)
    names = sorted(found)
    if len(names) > MAX_SKILLS:
        reg.warnings.append(f"stopped at the limit of {MAX_SKILLS} skills; "
                            f"{len(names) - MAX_SKILLS} more ignored")
        names = names[:MAX_SKILLS]
    kept, percent = _fit_menu({n: found[n] for n in names})
    if len(kept) < len(names):
        reg.warnings.append(f"stopped: the skill menu budget of {MAX_MENU} characters cannot "
                            f"hold even shortened descriptions; {len(names) - len(kept)} "
                            f"more ignored")
    if percent < 1000:
        reg.warnings.append(f"the skill menu budget of {MAX_MENU} characters is exceeded: "
                            f"descriptions longer than {MIN_DESCRIPTION} characters were "
                            f"shortened to {percent / 10:g}% of their length (the skill tool "
                            f"still loads the full instructions)")
    reg.skills = kept
    return reg


def _shorten(text, ratio_permille):
    """Cut a description to ratio/1000 of its length, never below MIN_DESCRIPTION, and
    never touching one that is already that short."""
    if len(text) <= MIN_DESCRIPTION:
        return text
    size = max(MIN_DESCRIPTION, len(text) * ratio_permille // 1000)
    return text if size >= len(text) else text[:size - 1].rstrip() + "…"


def _fit_menu(found):
    """-> ({name: Skill} that fit MAX_MENU, ratio in per-mille used). Over budget, the
    descriptions shrink in proportion (the largest ratio that fits); only when even the
    minimum descriptions overflow are skills dropped, from the end of the alphabet."""
    names = sorted(found)

    def size(chosen, ratio):
        # the lines are joined with newlines, so there is one fewer newline than lines
        return sum(len(f"- {n}: ") + len(_shorten(found[n].description, ratio)) + 1
                   for n in chosen) - 1

    while True:
        ratio = next((r for r in range(1000, -1, -1) if size(names, r) <= MAX_MENU), None)
        if ratio is not None or not names:
            break
        names = names[:-1]
    ratio = 1000 if ratio is None else ratio
    return {n: dataclasses.replace(found[n], description=_shorten(found[n].description, ratio))
            for n in names}, ratio


# ------------------------------------------------- what the model must not edit
def is_protected(path, reg):
    """True for anything inside a configured skills folder, existing or not yet. Also
    true under the folder's NEW name if it was renamed: a path prefix alone let
    `ren skills moved` + a write under `moved` through (found by the review)."""
    c = harness_hooks.canonical(path)
    if _inside(c, reg.roots):
        return True
    if not reg.root_ids:
        return False
    p = c
    while True:
        if _identity(p) in reg.root_ids:
            return True
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent


def mentions_protected(text, reg):
    """Shell strings (already lower-cased, ^ and quotes removed) that name SKILL.md or a
    skills folder, as a whole word anywhere (so `ren skills x`, `rmdir skills` and
    `mklink /J skills ..` are caught, and `echo skills-evil` or `myskills` are not).
    Best effort: a shell can still build the name from pieces."""
    if not reg.roots:
        return False
    names = {os.path.basename(r.rstrip("/" + chr(92))).lower() for r in reg.roots}
    names.discard("")
    names.add("skill.md")
    return harness_hooks.mentions_any(text, names)
