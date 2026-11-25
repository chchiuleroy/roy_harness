"""The outbox of the question-and-answer layer (stage S4): one JSON file per answer, never overwritten.

`ans-<ask id>.json` is created exclusively: the same content written again is a no-op (crash recovery may repeat a write), different content under
the same id is an error. A held answer is released by a second file, `ans-<ask id>.released.json`; the answer file itself never changes, so the
record of what the model said stays exactly what it was. Everything the model wrote is data (`untrusted: true`).
"""
import json
import os
import re
import tempfile
import time

from .errors import KbError

ID_PATTERN = re.compile(r"^ask_[0-9a-f]{16}$")


class OutboxError(KbError):
    pass


def check_id(ask_id):
    if not isinstance(ask_id, str) or not ID_PATTERN.fullmatch(ask_id):
        raise OutboxError("not an answer id")
    return ask_id


def path_of(outdir, ask_id, released=False):
    return os.path.join(outdir, f"ans-{check_id(ask_id)}" + (".released" if released else "") + ".json")


def _dump(payload):
    return (json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=1) + "\n").encode("ascii")


def _sweep(outdir, max_age=3600.0):
    """A hard crash can leave a temporary file; one older than `max_age` seconds belongs to nobody (a writer that is still working is younger)."""
    now = time.time()
    try:
        names = os.listdir(outdir)
    except OSError:
        return
    for name in names:
        if name.startswith(".tmp-"):
            path = os.path.join(outdir, name)
            try:
                if now - os.path.getmtime(path) > max_age:
                    os.remove(path)
            except OSError:
                pass


def _publish_exclusive(path, data, tolerate_other=False):
    """Create `path` with `data`. Returns True when this call created it. When it exists already: the same bytes are a no-op (False); other bytes are
    an error, unless `tolerate_other` (a release marker: the first release stands whoever comes second). A temporary file is made durable first and
    then linked into place (the link refuses to replace)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _sweep(os.path.dirname(path))
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            if not tolerate_other:
                with open(path, "rb") as f:
                    if f.read() != data:
                        raise OutboxError("an answer with this id exists and says something else") from None
            return False
    finally:
        os.remove(tmp)


def write_message(outdir, payload):
    _publish_exclusive(path_of(outdir, payload["ask_id"]), _dump(payload))


def release(outdir, ask_id, by):
    """Mark a held answer as looked at and accepted by a person. Returns True when this call released it, False when it was already released."""
    if not os.path.isfile(path_of(outdir, ask_id)):
        raise OutboxError("no such answer")
    path = path_of(outdir, ask_id, released=True)
    return _publish_exclusive(path, _dump({"kind": "release", "ask_id": ask_id, "by": str(by)[:80], "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}),
                              tolerate_other=True)


def read(outdir, ask_id):
    """The answer record with a computed `released` flag (the file itself is not changed)."""
    try:
        with open(path_of(outdir, ask_id), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise OutboxError("no such answer") from None
    data["released"] = os.path.exists(path_of(outdir, ask_id, released=True))
    return data


def list_ids(outdir):
    if not os.path.isdir(outdir):
        return []
    return sorted(n[4:-5] for n in os.listdir(outdir) if n.startswith("ans-") and n.endswith(".json") and ID_PATTERN.fullmatch(n[4:-5]))
