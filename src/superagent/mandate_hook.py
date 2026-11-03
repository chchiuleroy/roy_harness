"""PreToolUse hook that enforces a ticket's Mandate. Runs as its own process, started by the
harness from the ticket's `.harness.hooks.json`:

    python mandate_hook.py --mandate <path to mandate.json>      (JSON payload on stdin)

Exit 0 = no objection (the harness' own gate and `safe` mode still decide); exit 2 with the
reason on stderr = veto. A hook can only tighten, never grant. Any failure here is a veto:
the harness config also says onFailure=block. This file imports nothing from the harness or
from the rest of the package.
"""
import json
import os
import sys

# Tools a Slice 1 ticket never gets. Names from the harness' tool list, plus any MCP tool.
DENY = {"ask_user", "agent", "skill", "view_image", "monitor", "schedule_wakeup",
        "task_output", "task_stop"}
PASS = {"todo_write"}                    # only edits the model's own to-do list
CHECK_PATH = {"read_file", "write_file", "edit_file"}
FALSY = (None, "", False, "false", "False", "0", "no", 0)


def _real(path):
    return os.path.normcase(os.path.realpath(path))


def inside(path, root, base):
    """True when `path` (relative ones resolved against `base`) is root or below it."""
    if not isinstance(path, str) or not path:
        return False
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    try:
        return os.path.commonpath([_real(path), _real(root)]) == _real(root)
    except ValueError:                   # different drives
        return False


def decide(tool_name, tool_input, mandate, cwd=None):
    """-> None when there is no objection, else the reason for the veto."""
    root = mandate.get("root")
    if not isinstance(root, str) or not root:
        return "mandate has no root"
    if not isinstance(cwd, str) or not cwd:
        return "the hook payload has no cwd"
    if _real(cwd) != _real(root):
        return f"the harness runs in {cwd!r}, not in the ticket folder"
    if not isinstance(tool_input, dict):
        return "tool_input is not an object"
    if tool_name in PASS:
        return None
    if tool_name in DENY or "__" in str(tool_name):
        return f"tool {tool_name!r} is not part of this ticket's mandate"
    if tool_name in CHECK_PATH:
        if not inside(tool_input.get("path"), root, root):
            return f"{tool_name} outside the ticket folder is not allowed"
        return None
    if tool_name == "run_command":
        if tool_input.get("background") not in FALSY:
            return "background commands are not part of this ticket's mandate"
        return None
    return f"unknown tool {tool_name!r}"


def main(argv):
    try:
        if len(argv) != 3 or argv[1] != "--mandate":
            raise ValueError("usage: mandate_hook.py --mandate <mandate.json>")
        with open(argv[2], encoding="utf-8") as f:
            mandate = json.load(f)
        payload = json.load(sys.stdin)
        reason = decide(payload.get("tool_name"), payload.get("tool_input"), mandate,
                        cwd=payload.get("cwd"))
    except Exception as e:               # unreadable mandate, bad payload, anything
        reason = f"mandate hook could not decide ({type(e).__name__}: {e})"
    if reason:
        print(f"superagent mandate: {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
