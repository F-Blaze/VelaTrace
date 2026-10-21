"""Bounded HTTPS for the opt-in parts features (JLCPCB catalogue and live lookup, footprint fit).

Only the fixed hosts below can be contacted, over verified TLS, with no redirects, no cookies,
a hard size limit and a hard deadline. Nothing here runs unless the user clicked a button.
"""
from __future__ import annotations

import http.client
import socket
import ssl
from time import monotonic
from typing import Callable
from urllib.parse import urlsplit

from .errors import ValidationError
from .network import connect_before

# Every host this feature may contact. docs/privacy.md lists the same ones.
ALLOWED_HOSTS = frozenset({"bouni.github.io", "cart.jlcpcb.com", "easyeda.com", "api.digikey.com"})
# DigiKey takes the user's own API credentials, so only its token and keyword-search paths are open.
_DIGIKEY_POSTS = frozenset({"/v1/oauth2/token", "/products/v4/search/keyword"})
USER_AGENT = "VelaTrace-jlcpcb (+https://github.com/F-Blaze/VelaTrace)"
IDLE_TIMEOUT = 30.0


class JlcNetError(ValidationError):
    """The request was refused, failed or exceeded its limits; nothing was changed."""
    status = 0   # the HTTP status when the server answered with a refusal


class Cancelled(JlcNetError):
    pass


def _permitted(host: str | None, method: str, path: str) -> bool:
    if host == "api.digikey.com":
        return method == "POST" and path in _DIGIKEY_POSTS
    if host == "easyeda.com":
        return method == "GET"
    return host in ALLOWED_HOSTS and method in {"GET", "HEAD"}


def request(url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
            timeout: float = 30.0, max_bytes: int = 1_000_000,
            sink: Callable[[bytes], None] | None = None,
            cancel: Callable[[], bool] | None = None,
            body: bytes | None = None, content_type: str | None = None) -> tuple[int, dict[str, str], bytes]:
    """One request. Returns (status, lower-cased headers, body); with `sink` the body is
    streamed to it instead. Only 200 and 206 are accepted. POST needs `body` and `content_type`;
    other methods send neither."""
    parsed = urlsplit(url)
    sends = body is not None and bool(content_type) if method == "POST" else body is None and content_type is None
    if (parsed.scheme != "https" or not _permitted(parsed.hostname, method, parsed.path) or parsed.port
            or parsed.username or parsed.password or not sends):
        raise JlcNetError("Only the built-in parts data hosts can be contacted.")
    deadline = monotonic() + timeout
    connection = http.client.HTTPSConnection(parsed.hostname, 443, timeout=timeout,
                                             context=ssl.create_default_context())
    connection._create_connection = lambda address, timeout, source_address=None: connect_before(
        address, deadline, source_address)
    response = None
    try:
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        connection.request(method, path, body=body, headers={
            "User-Agent": USER_AGENT, "Accept-Encoding": "identity",
            **({"Content-Type": content_type} if content_type else {}), **(headers or {})})
        response = connection.getresponse()
        if response.status not in (200, 206):
            refused = JlcNetError(f"{parsed.hostname} answered HTTP {response.status}"
                                  + ("; redirects are refused." if 300 <= response.status < 400 else "."))
            refused.status = response.status
            raise refused
        found = {name.lower(): value for name, value in response.getheaders()}
        declared = found.get("content-length")
        if declared is not None and not declared.isdigit():
            raise JlcNetError("Server sent an invalid Content-Length.")
        if method == "HEAD":
            return response.status, found, b""
        if declared is not None and int(declared) > max_bytes:
            raise JlcNetError("Response exceeds the size limit.")
        body, received = bytearray(), 0
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError()
            if cancel is not None and cancel():
                raise Cancelled("Cancelled.")
            if connection.sock:
                connection.sock.settimeout(min(remaining, IDLE_TIMEOUT))
            chunk = response.read(262144)
            if not chunk:
                break
            received += len(chunk)
            if received > max_bytes:
                raise JlcNetError("Response exceeds the size limit.")
            if sink is not None:
                sink(chunk)
            else:
                body += chunk
        if declared is not None and int(declared) != received:
            raise JlcNetError("Download was cut short.")
        return response.status, found, bytes(body)
    except (TimeoutError, socket.timeout) as exc:
        raise JlcNetError(f"{parsed.hostname} timed out.") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise JlcNetError(f"Cannot reach {parsed.hostname}. Check the network connection.") from exc
    finally:
        if response is not None:
            response.close()
        connection.close()
