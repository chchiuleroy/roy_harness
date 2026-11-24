"""Raw-evidence store (Gate 2 section 4.3): content-addressed, never deleted.

copy mode: the bytes are copied into <blobs>/<aa>/<sha256> (temp file, fsync, atomic rename). ref mode: only the path and the
hash are recorded and the original stays where it is; verify() then reports honestly if it moved or changed.
There is deliberately no delete here: removal of user content is a proposal handled elsewhere (Gate 1 decisions 3 and 4).
The one place that removes a file is _discard_temp, and a test pins that.
"""
import hashlib
import os
import secrets
import stat
import time

from .durable import fsync_dir
from .errors import BlobError
from .faults import fault_point

CHUNK = 1024 * 1024
OK, MISSING, MISMATCH = "ok", "missing", "hash_mismatch"


class BlobRef:
    def __init__(self, sha256, size, mode, path, created):
        self.sha256, self.size, self.mode, self.path, self.created = sha256, size, mode, path, created

    def __repr__(self):
        return f"BlobRef({self.sha256[:12]}.., {self.size}, {self.mode})"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _norm(path):
    return os.path.abspath(path).replace(os.sep, "/")


def hash_file(path):
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            n += len(b)
    return h.hexdigest(), n


def _discard_temp(path):
    """The only deletion in this module: a temp file that never became a blob."""
    try:
        os.remove(path)
    except OSError:
        pass


class BlobStore:
    def __init__(self, blobs_dir):
        self.dir = os.path.abspath(blobs_dir)
        self.tmp = os.path.join(self.dir, "_tmp")
        os.makedirs(self.tmp, exist_ok=True)
        self._seen = {}                 # sha256 -> (path, size, mtime_ns) of the file at its last successful verify; used only when asked for (verify(cached=True))

    def blob_path(self, sha256):
        return os.path.join(self.dir, sha256[:2], sha256)

    # ------------------------------------------------------------------ writing
    def put_path(self, conn, path, mode="copy"):
        if mode not in ("copy", "ref"):
            raise BlobError(f"mode must be 'copy' or 'ref', not {mode!r}")
        if not os.path.isfile(path):
            raise BlobError(f"not a file: {path}")
        if mode == "ref":
            sha, size = hash_file(path)
            return self._record(conn, sha, size, "ref", _norm(path))
        return self._put_stream(conn, open(path, "rb"))

    def put_bytes(self, conn, data):
        import io
        return self._put_stream(conn, io.BytesIO(data))

    def _put_stream(self, conn, stream):
        tmp = os.path.join(self.tmp, secrets.token_hex(8))
        h, size = hashlib.sha256(), 0
        try:
            with stream, open(tmp, "wb") as out:
                while True:
                    b = stream.read(CHUNK)
                    if not b:
                        break
                    h.update(b)
                    size += len(b)
                    out.write(b)
                out.flush()
                os.fsync(out.fileno())
            fault_point("cas.after_temp_write")
            sha = h.hexdigest()
            final = self.blob_path(sha)
            fan_out = os.path.dirname(final)
            if not os.path.isdir(fan_out):
                os.makedirs(fan_out, exist_ok=True)
                fsync_dir(self.dir)                         # the new fan-out directory entry itself
            if os.path.exists(final):
                if hash_file(final) == (sha, size):
                    _discard_temp(tmp)                      # identical bytes are already stored
                else:                                       # a damaged copy: replace it with the good bytes just written
                    os.chmod(final, stat.S_IWRITE | stat.S_IREAD)
                    os.replace(tmp, final)
                    os.chmod(final, stat.S_IREAD)
                    fsync_dir(fan_out)
            else:
                os.replace(tmp, final)
                os.chmod(final, stat.S_IREAD)
                fsync_dir(fan_out)
        except BaseException:
            _discard_temp(tmp)
            raise
        fault_point("cas.before_row")
        return self._record(conn, sha, size, "copy", _norm(final))

    def _record(self, conn, sha, size, mode, path):
        cur = conn.execute("INSERT OR IGNORE INTO blobs(sha256,size,mode,path,first_seen) VALUES (?,?,?,?,?)",
                           (sha, size, mode, path, _now()))
        if cur.rowcount == 0:
            row = conn.execute("SELECT size, mode, path FROM blobs WHERE sha256=?", (sha,)).fetchone()
            if row[0] != size:
                raise BlobError(f"sha256 {sha} is recorded with size {row[0]} but this data has size {size}")
            if mode == "copy" and row[1] == "ref":
                raise BlobError("this content was recorded by reference earlier; copy mode over it needs an explicit upgrade (not in S0)")
            return BlobRef(sha, row[0], row[1], row[2], False)
        return BlobRef(sha, size, mode, path, True)

    # ------------------------------------------------------------------ checking
    def verify(self, conn, sha256, cached=False):
        """OK, MISSING or MISMATCH for the file that holds `sha256`. By default the file is read and hashed. With `cached=True` a file that passed an
        earlier verify by THIS object and still has the same path, size and modification time is taken as unchanged without reading it again: the
        guarantee becomes "unchanged since the last full check, as far as its size and time show", which a long-lived searcher accepts so that a hit does
        not cost a read of its whole original (a change that keeps both size and time is not seen until a full check, `kb doctor --deep` or `kb open`)."""
        row = conn.execute("SELECT size, mode, path FROM blobs WHERE sha256=?", (sha256,)).fetchone()
        if row is None:
            raise BlobError(f"unknown blob {sha256}")
        path = row[2] if row[1] == "ref" else self.blob_path(sha256)
        if not os.path.isfile(path):
            self._seen.pop(sha256, None)
            return MISSING
        st = os.stat(path)
        stamp = (path, st.st_size, st.st_mtime_ns)
        if cached and self._seen.get(sha256) == stamp:
            return OK
        sha, size = hash_file(path)
        if (sha, size) == (sha256, row[0]):
            self._seen[sha256] = stamp
            return OK
        self._seen.pop(sha256, None)
        return MISMATCH

    def recover(self):
        """Startup cleanup (call while holding the writer lock): temp files left by a crash never became blobs."""
        n = 0
        for name in os.listdir(self.tmp):
            _discard_temp(os.path.join(self.tmp, name))
            n += 1
        return n
