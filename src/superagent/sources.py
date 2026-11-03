"""Where events come from (Source) and where replies go (FileSink).

FileInbox watches a folder: one JSON file per event, `{"id", "thread", "sender", "text",
"addressed", "ts"}`. It never decides anything; idempotence is the Store's job (a trigger is
unique by (source, id)), so a file that is read twice is harmless.
"""
import json
import os
import shutil
import time
from typing import List, Protocol

from .model import Trigger

PARTIAL_GRACE = 2.0         # seconds: a file this young that does not parse may still be written


class Source(Protocol):
    id: str

    def poll(self) -> List[Trigger]:
        """New events. Must not change external state."""

    def settle(self, t: Trigger) -> None:
        """The event is in the database; the source may now forget it."""


class FileInbox:
    def __init__(self, folder, source_id=None):
        self.folder = os.path.abspath(folder)
        self.id = source_id or "file:" + os.path.basename(self.folder)
        self.done = os.path.join(self.folder, "done")
        self.bad = os.path.join(self.folder, "bad")
        for d in (self.folder, self.done, self.bad):
            os.makedirs(d, exist_ok=True)
        self._paths = {}

    def poll(self):
        out = []
        for name in sorted(os.listdir(self.folder)):
            path = os.path.join(self.folder, name)
            if not name.endswith(".json") or not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
                    raise ValueError("needs an object with a text string")
                ext_id = str(raw.get("id") or name)
                t = Trigger(self.id, ext_id, str(raw.get("thread") or ext_id),
                            str(raw.get("sender") or "unknown"), raw["text"],
                            bool(raw.get("addressed", False)), float(raw.get("ts") or time.time()))
            except (OSError, ValueError) as e:
                if time.time() - os.path.getmtime(path) < PARTIAL_GRACE:
                    continue                          # may still be being written: look again later
                self._quarantine(path, e)
                continue
            self._paths.setdefault(t.ext_id, []).append(path)
            out.append(t)
        return out

    def settle(self, t):
        for path in self._paths.pop(t.ext_id, []):
            if os.path.exists(path):
                os.replace(path, os.path.join(self.done, f"{time.time_ns()}-{os.path.basename(path)}"))

    def _quarantine(self, path, why):
        target = os.path.join(self.bad, os.path.basename(path))
        os.replace(path, target)
        with open(target + ".why.txt", "w", encoding="utf-8") as f:
            f.write(f"{type(why).__name__}: {why}\n")


class FileSink:
    """Replies as files: `<msg_id>.json`, written to a temp name and renamed, so a reader never
    sees half a file. The same msg_id always lands in the same file name; a consumer that has
    already taken the file and then sees it again after a crash recovery can tell by msg_id."""

    def __init__(self, folder):
        self.folder = os.path.abspath(folder)
        os.makedirs(self.folder, exist_ok=True)

    def write(self, msg_id, payload):
        body = dict(payload, msg_id=msg_id)
        tmp = os.path.join(self.folder, f".tmp-{msg_id}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(self.folder, f"{msg_id}.json"))
