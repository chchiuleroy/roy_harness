"""Static import analysis used by tests/test_layers.py (Gate 2 OPT1 / R1). Syntax-tree based, never text search:
a text search for 'kb' fails on documents that merely mention it and misses dynamic imports.

What it recognises (a GUARD for known syntactic forms, not a proof of independence): static and relative imports; dynamic imports
through importlib.import_module / __import__ including aliased names (import importlib as il; from importlib import import_module as im)
with literal module names; process launches through subprocess.* / os.system-style calls with literal command strings; and the presence
of exec / eval / compile and of dynamic imports with non-literal names, which are reported as findings because they cannot be analysed.
What it cannot see: process commands built from variables, code loaded from data, imports hidden behind getattr tricks.
The behavioural checks (tests/test_zero_cost.py, tools/removal_compare.py) cover part of that gap.
"""
import ast
import os
import sys

DYNAMIC_IMPORT_CALLS = {"importlib.import_module", "__import__", "importlib.__import__"}
PROCESS_CALLS = {"subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output",
                 "os.system", "os.popen", "os.execv", "os.execvp", "os.spawnv", "os.startfile"}
CODE_CALLS = {"exec", "eval", "compile"}


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def parse(path):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


def _aliases(tree):
    """Map a locally bound name to the real dotted name it stands for (import x as y / from x import y as z)."""
    m = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                m[(a.asname or a.name).split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for a in node.names:
                m[a.asname or a.name] = f"{node.module}.{a.name}"
    return m


def _resolve(name, aliases):
    if not name:
        return name
    head, _, rest = name.partition(".")
    real = aliases.get(head, head)
    return f"{real}.{rest}" if rest else real


def imports_of(tree, package=None):
    """Return (static, dynamic, process_strings, flags). flags: 'dynamic_nonconstant', 'dynamic_code'."""
    static, dynamic, procs, flags = set(), set(), set(), set()
    aliases = _aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            static.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package or ""
                if node.module:
                    static.add(f"{base}.{node.module}" if base else node.module)
                else:
                    static.update(f"{base}.{a.name}" if base else a.name for a in node.names)
            else:
                static.add(node.module or "")
        elif isinstance(node, ast.Call):
            name = _resolve(_dotted(node.func), aliases)
            args = list(node.args) + [k.value for k in node.keywords]
            strings = [c.value for a in args for c in ast.walk(a) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            if name in DYNAMIC_IMPORT_CALLS:
                dynamic.update(strings)
                if not (node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                    flags.add("dynamic_nonconstant")
            if name in PROCESS_CALLS:
                procs.update(strings)
            if name in CODE_CALLS:
                flags.add("dynamic_code")
    return static, dynamic, procs, flags


def py_files(root):
    for d, _, files in os.walk(root):
        if "__pycache__" in d:
            continue
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(d, f)


def check_kb_layers(src_pkg_dir, modules, import_allowed):
    """Return violations in a kb package directory: unmapped modules, stale table entries, and forbidden edges."""
    problems = []
    found = {os.path.splitext(f)[0] for f in os.listdir(src_pkg_dir) if f.endswith(".py")}
    for m in sorted(found - set(modules)):
        problems.append(("unmapped_module", m))
    for m in sorted(set(modules) - found):
        problems.append(("stale_table_entry", m))
    for m in sorted(found & set(modules)):
        static, dynamic, _, _ = imports_of(parse(os.path.join(src_pkg_dir, m + ".py")), package="kb")
        targets = {s.split(".")[1] for s in static if s.startswith("kb.") and len(s.split(".")) > 1}
        targets |= {d.split(".")[1] for d in dynamic if d.startswith("kb.") and len(d.split(".")) > 1}
        for t in sorted(targets):
            if t in modules and not import_allowed(modules[m], modules[t]):
                problems.append(("forbidden_edge", f"{m}({modules[m]}) -> {t}({modules[t]})"))
    return problems


def third_party_in_core(src_pkg_dir, modules):
    """T0 modules may import only the standard library and kb itself."""
    bad = []
    for m, tier in modules.items():
        if tier != "T0":
            continue
        static, dynamic, _, _ = imports_of(parse(os.path.join(src_pkg_dir, m + ".py")), package="kb")
        for s in static | dynamic:
            top = s.split(".")[0]
            if top and top != "kb" and top not in sys.stdlib_module_names:
                bad.append((m, s))
    return bad


def scan_harness(harness_dir, forbidden="kb"):
    """Return everything in the harness source that could depend on kb: imports, dynamic imports, process commands, unanalysable code."""
    local = {os.path.splitext(f)[0] for f in os.listdir(harness_dir) if f.endswith(".py")}
    problems = []
    for path in py_files(harness_dir):
        static, dynamic, procs, flags = imports_of(parse(path))
        name = os.path.basename(path)
        for s in static | dynamic:
            top = s.split(".")[0]
            if top == forbidden:
                problems.append((name, "imports", s))
            elif top and top not in sys.stdlib_module_names and top not in local:
                problems.append((name, "non_stdlib_import", s))
        for s in procs:
            words = s.replace("\\", "/").split()
            if s == forbidden or any(w == forbidden or w.startswith(forbidden + ".") for w in words) or ("-m" in words and forbidden in words):
                problems.append((name, "runs_process", s))
        for f in sorted(flags):
            problems.append((name, f, ""))
    return problems
