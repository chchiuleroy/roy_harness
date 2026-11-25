"""Durability helper: make a rename (a new directory entry) survive a system crash, not only a process kill.

A file's contents are fsynced by its writer; the directory entry that names it lives in the parent directory and is made durable by
syncing that directory. POSIX: fsync a descriptor for the directory. Windows: there is no portable equivalent; this tries the
documented route (FlushFileBuffers on a directory handle opened with backup semantics) and treats failure as 'best effort'.
What is tested: process-kill recovery (tests/test_db, test_cas_audit_profile) and that every atomic-replace path calls this.
What cannot be tested here: real power loss, on either OS.
"""
import os


def fsync_dir(path):
    """Returns True if the directory was synced, False if this platform/handle refused (best effort)."""
    path = os.path.abspath(path)
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        h = k32.CreateFileW(path, 0x40000000, 0x7, None, 3, 0x02000000, None)      # GENERIC_WRITE, share all, OPEN_EXISTING, BACKUP_SEMANTICS
        if h in (None, wintypes.HANDLE(-1).value):
            return False
        try:
            return bool(k32.FlushFileBuffers(h))
        finally:
            k32.CloseHandle(h)
    except (OSError, AttributeError):
        return False
