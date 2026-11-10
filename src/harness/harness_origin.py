"""Where did a host come from? Provenance for the gate (same folder as harness_vllm).

An injected instruction has to NAME its destination, and the injection arrives inside a tool
result (a file, a web page, command output). A host that shows up in tool output and was never
in the user's own words is therefore the cheapest reliable sign of an action somebody else
chose. This is deterministic: no model decides it, and a model cannot talk its way past it.

What is tracked: hosts (URLs, user@host, IPv4, and bare domains with a common TLD). File paths
are NOT tracked: reading a directory listing and then using a file name from it is normal work.
A host the model names from its own knowledge (pypi.org) appears in neither set and is not
flagged. Nothing here imports the harness.
"""
import re
from collections import deque

_URL = re.compile(r"[a-z][a-z0-9+.-]*://(?:[^/\s@]*@)?([a-z0-9._-]+)", re.I)
_AT_HOST = re.compile(r"[\w.+-]+@([a-z0-9-]+(?:\.[a-z0-9-]+)+)", re.I)        # mail, ssh, git@host:path
_IPV4 = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?![\w-]|\.\w)")
TLDS = ("com", "net", "org", "io", "dev", "ai", "app", "co", "info", "biz", "xyz", "top", "site",
        "online", "cloud", "me", "us", "uk", "de", "fr", "jp", "cn", "ru", "tw", "in", "example")
# A bare domain must not be the front of a longer dotted name: `foo.test.js` is a file.
_BARE = re.compile(r"(?<![\w@./:-])((?:[a-z0-9-]+\.)+(?:" + "|".join(TLDS) + r"))(?![\w-]|\.\w)", re.I)
LOOPBACK = ("localhost", "0.0.0.0")
MAX_HOSTS = 5000                          # a long session must not grow this without bound
MAX_RECENT, RECENT_CLIP = 8, 160


def hosts_in(text):
    """-> set of lower-case host names found in `text` (loopback excluded)."""
    if not isinstance(text, str) or not text:
        return set()
    found = set()
    for rx in (_URL, _AT_HOST, _IPV4, _BARE):
        found.update(m.group(1).lower().strip(".") for m in rx.finditer(text))
    return {h for h in found if h and h not in LOOPBACK and not h.startswith("127.")}


class Origin:
    """Hosts the user typed, hosts that appeared in tool output, and what ran this turn."""

    def __init__(self):
        self.clear()

    def clear(self):
        self.user, self.tool = set(), set()
        self.flooded = False              # the tool set hit its cap: stop trusting its silence
        self.recent = deque(maxlen=MAX_RECENT)

    @staticmethod
    def _room(known, found):
        """The part of `found` that fits under MAX_HOSTS, and whether something was left out."""
        new = sorted(found - known)
        room = max(MAX_HOSTS - len(known), 0)
        return set(new[:room]), len(new) > room

    def user_text(self, text):
        add, _ = self._room(self.user, hosts_in(text))
        self.user |= add                  # a host that did not fit is simply not "typed": it asks

    def tool_text(self, text):
        add, left_out = self._room(self.tool, hosts_in(text))
        self.tool |= add
        self.flooded = self.flooded or left_out

    def untrusted_hosts(self, text):
        """Hosts in `text` that appeared in tool output and were never in the user's words.
        An exact match: a subdomain of a host the user typed is not the user's (shared hosts such
        as github.io or amazonaws.com hand subdomains to strangers). Once the tool set overflowed
        it can no longer say what it has NOT seen, so every host the user did not type counts."""
        return sorted(h for h in hosts_in(text)
                      if (self.flooded or h in self.tool) and h not in self.user)

    # --- what ran since the last thing the user typed (the classifier reads this)
    def new_turn(self):
        self.recent.clear()

    def act(self, line):
        line = " ".join(str(line).split())
        self.recent.append(line if len(line) <= RECENT_CLIP else line[:RECENT_CLIP - 1] + "…")

    def recent_actions(self):
        return list(self.recent)
