"""Single-writer lock (Gate 2 F11, T-W1).

An operating-system byte-range lock on <home>/writer.lock: the OS drops it when the holding process dies, so there is no
stale-lock cleanup to get wrong. Readers never take it (SQLite WAL lets them read while a writer works).
The pid is written to a separate file for diagnostics only; the lock itself is the truth.
"""
import os

from .errors import WriterBusy

if os.name == "nt":
    import msvcrt

    def _lock(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fd):
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)


class WriterLock:
    def __init__(self, home):
        self.path = os.path.join(home, "writer.lock")
        self.pid_path = os.path.join(home, "writer.pid")
        self.fd = None

    def acquire(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
        try:
            _lock(fd)
        except OSError:
            os.close(fd)
            raise WriterBusy(f"another process holds the kb writer lock ({self._holder()})") from None
        self.fd = fd
        with open(self.pid_path, "w", encoding="utf-8", newline="") as f:
            f.write(str(os.getpid()))
        return self

    def release(self):
        if self.fd is not None:
            _unlock(self.fd)
            os.close(self.fd)
            self.fd = None

    def _holder(self):
        try:
            with open(self.pid_path, encoding="utf-8") as f:
                return "pid " + f.read().strip()
        except OSError:
            return "holder unknown"

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
