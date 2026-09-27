import socket
from threading import Event
from time import monotonic
import unittest
from unittest.mock import patch

from velatrace.network import connect_before
from velatrace.provider import Provider, ProviderConfig, ProviderError


class NetworkDeadlineTests(unittest.TestCase):
    def test_stalled_headers_are_cut_off_at_overall_deadline(self):
        shutdown = Event()

        class Connection:
            def __init__(self, *args, **kwargs):
                self.sock = self
            def connect(self):
                pass
            def settimeout(self, timeout):
                pass
            def request(self, *args):
                pass
            def getresponse(self):
                shutdown.wait(2)
                raise OSError("closed")
            def shutdown(self, how):
                shutdown.set()
            def close(self):
                pass

        provider = Provider(ProviderConfig("fixture", "https://example.invalid", "fixture", "fixture"),
                            disclosure_gate=lambda _: True)
        with patch("velatrace.provider.http.client.HTTPSConnection", Connection):
            start = monotonic()
            with self.assertRaisesRegex(ProviderError, "timed out"):
                provider._request("/fixture", {}, .05)
            self.assertTrue(shutdown.is_set())
            self.assertLess(monotonic() - start, .5)

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
