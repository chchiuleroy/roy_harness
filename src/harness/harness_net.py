"""Network policy shared by the harness: which URLs may be used, and no redirects.

Two measured facts drive this (Python 3.13.7, 2026-10-21):
  * urllib follows 301/302/303 and keeps the Authorization header, so a redirecting
    endpoint receives the API key and can answer in place of the real server.
  * Plain http is normal on a corporate network, so it is allowed only when the
    user switches it on explicitly (config file or HARNESS_ALLOW_HTTP=1), never
    because of anything a server, a model or a web page says.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def check_url(url, allow_http=False):
    """Return url if it may be used, else raise ValueError.
    https is always fine; http only for loopback or when allow_http is set."""
    try:
        parts = urllib.parse.urlsplit(url)        # handles case, userinfo, IPv6
        host = parts.hostname
    except ValueError:
        raise ValueError("unparsable URL")
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("URL must be http(s)://host/...")
    if parts.scheme == "http" and not allow_http and host not in LOCAL_HOSTS:
        raise ValueError("refusing plain http to a non-local host "
                         "(set allow_http / HARNESS_ALLOW_HTTP=1 to accept the risk)")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                               # 3xx then surfaces as HTTPError


_OPENER = urllib.request.build_opener(_NoRedirect)


def open_url(req, timeout):
    """Like urlopen, but a redirect raises urllib.error.HTTPError instead of being
    followed (so credentials never travel to another host)."""
    return _OPENER.open(req, timeout=timeout)


def post_json(url, body, headers=None, timeout=30, allow_http=False):
    """POST body as JSON; return (status, text). Non-2xx (including a refused
    redirect) comes back as a status, not an exception; connection errors and
    timeouts still raise (OSError / urllib.error.URLError)."""
    check_url(url, allow_http)
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("ascii"),     # ASCII-escaped: survives lone surrogates
        headers={"content-type": "application/json",
                 "user-agent": "harness101/0.3", **(headers or {})})
    try:
        with open_url(req, timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:2000].decode("utf-8", "replace")
