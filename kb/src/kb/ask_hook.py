"""PreToolUse hook for a question-and-answer ticket: it vetoes EVERY tool call (stage S4).

The model of an `ask` ticket only writes an answer; it calls nothing. The harness starts this file as its own process from the ticket's
`.harness.hooks.json` (`onFailure: block`), passes the call as JSON on stdin, and treats exit code 2 as a veto with the reason on stderr.
A hook can only tighten, never grant, and any failure here is a veto too. This file imports nothing from the harness or from the rest of kb.
"""
import json
import sys


def main():
    try:
        payload = json.load(sys.stdin)
        name = payload.get("tool_name") if isinstance(payload, dict) else None
        reason = f"kb ask: the model has no tools (it tried {str(name)[:60]!r})"
    except Exception as e:                       # unreadable payload: still a veto
        reason = f"kb ask: the hook could not read the call ({type(e).__name__}); no tool is allowed"
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
