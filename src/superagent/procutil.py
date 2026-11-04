"""Small process helpers that the standard library does not have: when a process started, and
what its command line is. Windows only (the harness itself is only tested on Windows 11); on
other systems both return None, and callers treat None as "cannot verify, do not touch".
"""
import os
import subprocess
import sys

# PowerShell writes to the console code page (cp950 here) unless told otherwise; decoding that as UTF-8
# raised UnicodeDecodeError in a reader thread (printed as a traceback by `down`)
UTF8_OUT = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    QUERY_LIMITED = 0x1000
    _EPOCH_DELTA = 11644473600                       # seconds from 1601 to 1970


def create_time(pid):
    """Seconds since the epoch when `pid` started, or None if there is no such process."""
    if os.name != "nt":
        return None
    handle = _k32.OpenProcess(QUERY_LIMITED, False, int(pid))
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not _k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                    ctypes.byref(kernel), ctypes.byref(user)):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        if (exited.dwHighDateTime << 32 | exited.dwLowDateTime) != 0:
            return None                              # already exited, handle kept alive by someone
        return ticks / 10_000_000 - _EPOCH_DELTA
    finally:
        _k32.CloseHandle(handle)


def cmdline(pid):
    """The command line of `pid`, or None."""
    if os.name != "nt":
        return None
    ps = os.path.join(os.environ.get("SystemRoot", "C:/Windows"), "System32", "WindowsPowerShell",
                      "v1.0", "powershell.exe")
    script = UTF8_OUT + f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"
    try:
        out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = out.stdout.strip()
    return text or None


def is_ours(pid, expected_create_time, must_contain, tolerance=2.0):
    """True only if `pid` is the very process we recorded: same start time (so the pid was not
    reused by something else) and a command line that names our home."""
    now = create_time(pid)
    if now is None or abs(now - expected_create_time) > tolerance:
        return False
    line = cmdline(pid)
    return bool(line) and os.path.normcase(must_contain) in os.path.normcase(line)


def find_by_cmdline(*fragments):
    """Pids of processes whose command line contains EVERY fragment (case-insensitive), not
    counting the scan itself. Used by the emergency path when the database cannot be trusted;
    callers pass enough fragments that only their own processes can match."""
    if os.name != "nt" or not fragments:
        return []
    ps = os.path.join(os.environ.get("SystemRoot", "C:/Windows"), "System32", "WindowsPowerShell",
                      "v1.0", "powershell.exe")
    tests = " -and ".join("$_.CommandLine.ToLower().Contains('" + os.path.normcase(f).replace("'", "''") + "')"
                          for f in fragments)
    script = (UTF8_OUT + "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and "
              "$_.CommandLine -and " + tests + " } | ForEach-Object { $_.ProcessId }")
    try:
        out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
