"""Deadline-bound connection setup, including platform DNS lookup.

A timed-out DNS worker can finish later, but it never opens a socket or holds
request data. HTTPS requests remain on the calling thread and stop on timeout.
"""
from queue import Empty, Queue
import socket
from threading import BoundedSemaphore, Thread
from time import monotonic


_resolvers = BoundedSemaphore(4)


def connect_before(address, deadline: float, source_address=None):
    remaining = deadline - monotonic()
    if remaining <= 0 or not _resolvers.acquire(blocking=False):
        raise TimeoutError("Endpoint resolution unavailable within request deadline")
    resolved = Queue(maxsize=1)

    def resolve():
        try:
            resolved.put(socket.getaddrinfo(address[0], address[1], 0, socket.SOCK_STREAM))
        except OSError as exc:
            resolved.put(exc)
        finally:
            _resolvers.release()

    Thread(target=resolve, daemon=True, name="velatrace-endpoint-dns").start()
    try:
        addresses = resolved.get(timeout=remaining)
    except Empty as exc:
        raise TimeoutError("Endpoint resolution timed out") from exc
    if isinstance(addresses, Exception):
        raise addresses
    last_error = OSError("Endpoint has no addresses")
    for family, kind, protocol, _, target in addresses:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("Connection deadline exceeded")
        candidate = socket.socket(family, kind, protocol)
        try:
            candidate.settimeout(remaining)
            if source_address:
                candidate.bind(source_address)
            candidate.connect(target)
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Connection deadline exceeded")
            candidate.settimeout(remaining)
            return candidate
        except OSError as exc:
            last_error = exc
            candidate.close()
    raise last_error
