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
MAX_MENU = 8_000
MAX_BODY = 20_000
MAX_FILE = 1_000_000        # bytes read from one SKILL.md
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
    return any(real == r or real.startswith(r + os.sep) for r in roots)


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
        if not _inside(harness_hooks.canonical(skill.folder), self.roots):
            return (f"ERROR: skill {skill.name} now resolves outside the skills folders "
                    f"(its folder was replaced by a link); refusing to load it")
        text, why = _read_text(skill.path)
        if text is None:
            return f"ERROR: skill {skill.name}: {why}"
        body = parse_frontmatter(text)[1].strip("\n")
        if len(body) > MAX_BODY:
            body = (body[:MAX_BODY] + f"\n... [truncated {len(body) - MAX_BODY} more chars; "
                    f"read the full file with read_file: {skill.path}] ...")
        return f"[skill {skill.name}; its folder is {os.path.abspath(skill.folder)}]\n{body}"


EMPTY = Registry()


def load_all(dirs=None):
    """Scan the skills folders (default: HARNESS_SKILLS_DIRS, else ./skills). One broken
    skill is skipped with a warning; it never stops the others."""
    if dirs is None:
        env = os.environ.get("HARNESS_SKILLS_DIRS")
        dirs = [d for d in env.split(os.pathsep) if d.strip()] if env else ["skills"]
    reg = Registry()
    reg.roots = [harness_hooks.canonical(d) for d in dirs]
    used = 0
    for root in dirs:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            folder = os.path.join(root, name)
            md = os.path.join(folder, "SKILL.md")
            if not os.path.isdir(folder) or not os.path.isfile(md):
                continue
            if not _inside(harness_hooks.canonical(folder), reg.roots):
                reg.warnings.append(
                    f"skipped {name}: it is a link that leaves the skills folders (it points "
                    f"to {os.path.realpath(folder)}); add that folder to HARNESS_SKILLS_DIRS "
                    f"to use it")
                continue
            if not NAME_RE.match(name):
                reg.warnings.append(f"skipped {name!r}: the folder name must be 1-64 of "
                                    f"letters, digits, '.', '_' or '-', not starting with one "
                                    f"of the last three")
                continue
            text, why = _read_text(md)
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
            if name in reg.skills:
                reg.warnings.append(f"skipped {name} from {root}: a skill with that name "
                                    f"was already loaded from {reg.skills[name].folder}")
                continue
            if len(reg.skills) >= MAX_SKILLS:
                reg.warnings.append(f"stopped at the limit of {MAX_SKILLS} skills; the rest "
                                    f"are ignored")
                return _finish(reg)
            line = len(f"- {name}: {description}") + 1
            if used + line > MAX_MENU:
                reg.warnings.append(f"stopped: the skill menu budget of {MAX_MENU} characters "
                                    f"is used up; the rest are ignored")
                return _finish(reg)
            used += line
            reg.skills[name] = Skill(name, description, folder, md)
    return _finish(reg)


def _finish(reg):
    reg.skills = dict(sorted(reg.skills.items()))
    return reg


# ------------------------------------------------- what the model must not edit
def is_protected(path, reg):
    """True for anything inside a configured skills folder, existing or not yet."""
    return _inside(harness_hooks.canonical(path), reg.roots)


def mentions_protected(text, reg):
    """Shell strings (already lower-cased, ^ and quotes removed) that name SKILL.md or a
    skills folder followed by a path separator (whole-name match; best effort)."""
    if not reg.roots:
        return False
    if harness_hooks.mentions_any(text, {"skill.md"}):
        return True
    names = {os.path.basename(r.rstrip("/" + chr(92))).lower() for r in reg.roots}
    names.discard("")
    pattern = r"(?<![\w.-])(?:" + "|".join(re.escape(n) for n in names) + r")[\\/]"
    return re.search(pattern, text) is not None
