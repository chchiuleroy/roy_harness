"""Test fault points. With no KB_FAULT environment variable these calls do nothing.

KB_FAULT=<name>        -> kill the process abruptly at that point (exit code 137, no cleanup, no finally blocks)
KB_FAULT=<name>:raise  -> raise FaultInjected there instead

Tests start a subprocess with the variable set to prove that a crash at a given step leaves a recoverable state.
The point names are part of the test contract; keep them stable.
"""
import os

from .errors import FaultInjected

KILL_EXIT_CODE = 137


def fault_point(name):
    spec = os.environ.get("KB_FAULT")
    if not spec:
        return
    target, _, mode = spec.partition(":")
    if target != name:
        return
    if mode == "raise":
        raise FaultInjected(name)
    os._exit(KILL_EXIT_CODE)
