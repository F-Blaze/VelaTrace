import socket
from threading import Event
from time import monotonic
import unittest
from unittest.mock import patch

from velatrace.network import connect_before


class NetworkDeadlineTests(unittest.TestCase):
    def test_dns_timeout_never_opens_socket(self):
        release = Event()

        def slow_lookup(*_):
            release.wait(2)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

        with patch("velatrace.network.socket.getaddrinfo", side_effect=slow_lookup), \
                patch("velatrace.network.socket.socket") as connection:
            start = monotonic()
            try:
                with self.assertRaises(TimeoutError):
                    connect_before(("example.invalid", 443), start + 0.05)
                self.assertLess(monotonic() - start, 0.5)
                connection.assert_not_called()
            finally:
                release.set()

    def test_expired_deadline_does_not_resolve(self):
        with patch("velatrace.network.socket.getaddrinfo") as lookup:
            with self.assertRaises(TimeoutError):
                connect_before(("example.invalid", 443), monotonic() - 1)
            lookup.assert_not_called()
