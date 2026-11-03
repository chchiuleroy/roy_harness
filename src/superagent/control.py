"""How a person talks to a running orchestrator: command files in `<home>/control/`.

The CLI (`answer`, `stop`, `resume`, `shutdown`) cannot touch the database or the child
processes itself; the running engine owns both. So it drops a small JSON file, the engine takes
it on its next pass and writes the outcome next to it, and the CLI waits for that outcome.
"""
import json
import os
import time
import uuid

COMMANDS = ("answer", "stop", "resume", "shutdown")


class Channel:
    def __init__(self, home):
        self.dir = os.path.join(os.path.abspath(home), "control")
        os.makedirs(self.dir, exist_ok=True)

    def submit(self, cmd, **args):
        if cmd not in COMMANDS:
            raise ValueError(f"unknown command {cmd!r}")
        name = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        tmp = os.path.join(self.dir, f".{name}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(args, cmd=cmd), f)
        os.replace(tmp, os.path.join(self.dir, f"{name}.cmd.json"))
        return name

    def wait(self, name, timeout=30.0):
        path = os.path.join(self.dir, f"{name}.result.json")
        end = time.time() + timeout
        while time.time() < end:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            time.sleep(0.1)
        return None

    def take(self):
        """-> [(name, command dict)] in submission order; each file is consumed."""
        out = []
        for fname in sorted(os.listdir(self.dir)):
            if not fname.endswith(".cmd.json"):
                continue
            path = os.path.join(self.dir, fname)
            name = fname[:-len(".cmd.json")]
            try:
                with open(path, encoding="utf-8") as f:
                    cmd = json.load(f)
            except (OSError, ValueError):
                cmd = {"cmd": "invalid"}
            os.remove(path)
            out.append((name, cmd))
        return out

    def reply(self, name, ok, message=""):
        tmp = os.path.join(self.dir, f".{name}.res.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ok": bool(ok), "message": message}, f)
        os.replace(tmp, os.path.join(self.dir, f"{name}.result.json"))
