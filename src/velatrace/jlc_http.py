"""Bounded HTTPS for the two opt-in JLCPCB features (catalogue download, live part lookup).

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

# Every host this feature may contact. docs/privacy.md lists the same two.
ALLOWED_HOSTS = frozenset({"bouni.github.io", "cart.jlcpcb.com"})
USER_AGENT = "VelaTrace-jlcpcb (+https://github.com/F-Blaze/VelaTrace)"
IDLE_TIMEOUT = 30.0


class JlcNetError(ValidationError):
    """The request was refused, failed or exceeded its limits; nothing was changed."""


class Cancelled(JlcNetError):
    pass


def request(url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
            timeout: float = 30.0, max_bytes: int = 1_000_000,
            sink: Callable[[bytes], None] | None = None,
            cancel: Callable[[], bool] | None = None) -> tuple[int, dict[str, str], bytes]:
    """One request. Returns (status, lower-cased headers, body); with `sink` the body is
    streamed to it instead. Only 200 and 206 are accepted."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.port
            or parsed.username or parsed.password or method not in {"GET", "HEAD"}):
        raise JlcNetError("Only the built-in JLCPCB data hosts can be contacted.")
    deadline = monotonic() + timeout
    connection = http.client.HTTPSConnection(parsed.hostname, 443, timeout=timeout,
                                             context=ssl.create_default_context())
    connection._create_connection = lambda address, timeout, source_address=None: connect_before(
        address, deadline, source_address)
    response = None
    try:
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        connection.request(method, path, headers={
            "User-Agent": USER_AGENT, "Accept-Encoding": "identity", **(headers or {})})
        response = connection.getresponse()
        if response.status not in (200, 206):
            raise JlcNetError(f"{parsed.hostname} answered HTTP {response.status}"
                              + ("; redirects are refused." if 300 <= response.status < 400 else "."))
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
