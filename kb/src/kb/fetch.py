"""Fetching a pasted URL (Gate 1 W1 'paste URL'). This is the one place that talks to the network on kb's own initiative, and only because the
user pasted the address. Rules: http and https only; redirects must also be http(s); a host that resolves to a private, loopback,
link-local or reserved address is refused unless the caller says allow_private (so a later agent-facing path cannot be turned into a
request to the local network); size and time are capped; the bytes are returned untouched and stored as a snapshot by the caller."""
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

from .errors import KbError

MAX_BYTES = 20 * 1024 * 1024
TIMEOUT_S = 30
CONTENT_SUFFIX = {"text/html": ".html", "application/xhtml+xml": ".html", "text/markdown": ".md", "text/plain": ".txt",
                  "text/csv": ".csv", "text/tab-separated-values": ".tsv"}


class FetchRefused(KbError):
    pass


def check_url(url, allow_private=False, resolver=socket.getaddrinfo):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchRefused(f"only http and https URLs are fetched, not {parts.scheme or 'this'}")
    if allow_private:
        return
    try:
        infos = resolver(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as e:
        raise FetchRefused(f"cannot resolve {parts.hostname}: {e}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise FetchRefused(f"{parts.hostname} resolves to a non-public address ({ip}); pass allow_private to fetch it anyway")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_private, resolver):
        self.allow_private, self.resolver = allow_private, resolver

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl, self.allow_private, self.resolver)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, allow_private=False, resolver=socket.getaddrinfo, max_bytes=MAX_BYTES):
    """Returns (bytes, content_type, final_url)."""
    check_url(url, allow_private, resolver)
    opener = urllib.request.build_opener(_CheckedRedirects(allow_private, resolver))
    req = urllib.request.Request(url, headers={"User-Agent": "kb/0 (personal knowledge layer)"})
    try:
        with opener.open(req, timeout=TIMEOUT_S) as r:
            data = r.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise FetchRefused(f"the page is larger than {max_bytes} bytes")
            ctype = (r.headers.get_content_type() or "").lower()
            return data, ctype, r.geturl()
    except urllib.error.URLError as e:
        raise FetchRefused(f"could not fetch {url}: {e}") from None


def filename_hint(url, content_type):
    path = urllib.parse.urlsplit(url).path
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        name = (name or "index") + CONTENT_SUFFIX.get(content_type, ".html" if content_type.startswith("text/") else "")
    return name
