"""The JLCPCB network boundary: fixed hosts, bounded, no board data, offline unless clicked."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jlc_fixture import install
from velatrace import jlc_catalog, jlc_http, jlc_live, jlc_parts
from velatrace.jlc_http import JlcNetError
from velatrace.models import Component, DesignSnapshot, Pin

DOCS = Path(__file__).parent.parent / "docs"


class Response:
    def __init__(self, status=200, body=b"hello", headers=None):
        self.status, self.body = status, body
        self.headers = {"Content-Length": str(len(body))} if headers is None else headers

    def getheaders(self):
        return list(self.headers.items())

    def read(self, size):
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk

    def close(self):
        pass


class Connection:
    sent = []
    bodies = []
    response = Response()

    def __init__(self, host, port, timeout=None, context=None):
        assert context is not None and context.verify_mode.name == "CERT_REQUIRED" and context.check_hostname
        self.host, self.sock = host, None

    def request(self, method, path, body=None, headers=None):
        Connection.sent.append((self.host, method, path, dict(headers)))
        Connection.bodies.append(body)

    def getresponse(self):
        return Connection.response

    def close(self):
        pass


class RequestTests(unittest.TestCase):
    def call(self, response, url="https://cart.jlcpcb.com/x?componentCode=C1", **kwargs):
        Connection.sent, Connection.response = [], response
        with patch.object(jlc_http.http.client, "HTTPSConnection", Connection):
            return jlc_http.request(url, **kwargs)

    def test_request_carries_only_the_url_and_a_plain_user_agent(self):
        status, headers, body = self.call(Response())
        self.assertEqual((status, body, headers["content-length"]), (200, b"hello", "5"))
        host, method, path, sent = Connection.sent[0]
        self.assertEqual((host, method, path), ("cart.jlcpcb.com", "GET", "/x?componentCode=C1"))
        self.assertEqual(set(sent), {"User-Agent", "Accept-Encoding"})
        self.assertTrue(sent["User-Agent"].startswith("VelaTrace-jlcpcb"))

    def test_redirects_errors_oversize_and_truncation_are_refused(self):
        for response in (Response(302, b"", {"Location": "https://evil.example/"}), Response(403),
                         Response(body=b"x" * 50), Response(headers={"Content-Length": "9"}),
                         Response(headers={"Content-Length": "lots"})):
            with self.assertRaises(JlcNetError):
                self.call(response, max_bytes=10)

    def test_only_the_documented_method_and_path_per_host_is_allowed(self):
        token, keyword = "https://api.digikey.com/v1/oauth2/token", "https://api.digikey.com/products/v4/search/keyword"
        post = {"method": "POST", "body": b"x=1", "content_type": "application/x-www-form-urlencoded"}
        self.assertEqual(self.call(Response(), token, **post)[0], 200)
        host, method, path, sent = Connection.sent[0]
        self.assertEqual((host, method, path, Connection.bodies[0]), ("api.digikey.com", "POST", "/v1/oauth2/token", b"x=1"))
        self.assertEqual(sent["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(self.call(Response(), keyword, **{**post, "content_type": "application/json"})[0], 200)
        self.assertEqual(self.call(Response(), "https://easyeda.com/api/products/C1/components?version=6.4.19.5")[0], 200)
        self.assertEqual(Connection.sent[0][1], "GET")
        for url, extra in (
                (token, {}),                                                     # POST needs a body and a content type
                (token, {"method": "POST", "body": b"x"}),
                (token, {"method": "POST", "content_type": "text/plain"}),
                (token, {"method": "GET"}),                                      # wrong method for DigiKey
                (token, {"method": "HEAD"}),
                ("https://api.digikey.com/products/v4/search/keyword/productdetails", post),   # other paths
                ("https://api.digikey.com/v1/oauth2/token/", post),
                ("https://api.digikey.com/products/v4/search/keyword?x=1&", {"method": "GET"}),
                ("https://api.digikey.com/anything", post),
                ("https://easyeda.com/api/x", {"method": "POST", "body": b"x", "content_type": "text/plain"}),
                ("https://easyeda.com/api/x", {"method": "HEAD"}),               # EasyEDA is GET only
                ("https://easyeda.com/api/x", {"body": b"x"}),                   # GET never carries a body
                ("https://cart.jlcpcb.com/x", {"method": "POST", "body": b"x", "content_type": "text/plain"}),
                ("https://sandbox.digikey.com/v1/oauth2/token", post),           # other hosts
                ("https://api.digikey.com.evil.example/v1/oauth2/token", post),
                ("http://api.digikey.com/v1/oauth2/token", post),
                ("https://api.digikey.com:444/v1/oauth2/token", post)):
            Connection.sent.clear()
            with self.assertRaises(JlcNetError, msg=(url, extra)):
                self.call(Response(), url, **extra)
            self.assertEqual(Connection.sent, [])

    def test_redirects_are_refused_for_the_new_hosts_and_carry_the_status(self):
        for url, extra in (("https://easyeda.com/api/x", {}), ("https://api.digikey.com/v1/oauth2/token",
                           {"method": "POST", "body": b"x", "content_type": "text/plain"})):
            with self.assertRaisesRegex(JlcNetError, "redirects are refused"):
                self.call(Response(302, b"", {"Location": "https://evil.example/"}), url, **extra)
        with self.assertRaises(JlcNetError) as caught:
            self.call(Response(401))
        self.assertEqual(caught.exception.status, 401)

    def test_streaming_and_head(self):
        chunks = []
        self.assertEqual(self.call(Response(206, b"abcdef"), sink=chunks.append)[2], b"")
        self.assertEqual(b"".join(chunks), b"abcdef")
        self.assertEqual(self.call(Response(body=b"", headers={"Content-Length": "80000000"}),
                                   method="HEAD", max_bytes=1)[1]["content-length"], "80000000")
        with self.assertRaises(jlc_http.Cancelled):
            self.call(Response(body=b"abc"), cancel=lambda: True)


class OfflineByDefaultTests(unittest.TestCase):
    def test_checks_and_tables_never_open_a_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)
            install(config).close()
            snapshot = DesignSnapshot((Component("R1", "10k", "Resistor_SMD:R_0402_1005Metric",
                                                 (Pin("1", "A"), Pin("2", "GND")), {"LCSC": "C375503"}),), "test")
            with patch.object(jlc_http.http.client, "HTTPSConnection", side_effect=AssertionError("network")), \
                    patch.object(jlc_http, "connect_before", side_effect=AssertionError("network")):
                rules = {item.rule for item in jlc_parts.design_findings(snapshot, config)}
                catalogue = jlc_catalog.load(config)
                catalogue.search("10kΩ")
                catalogue.close()
                jlc_live.load_live(config)
            self.assertIn("bom.lcsc_mismatch", rules)

    def test_cached_only_heavy_checks_never_open_a_connection(self):
        from velatrace import part_lookup
        from velatrace.audit_rules import run_heavy
        header = Component("J1", "Conn_01x02", "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
                           (Pin("1", "A", offset_mm=(0.0, 0.0)), Pin("2", "B", offset_mm=(0.0, 2.54))),
                           {"LCSC": "C65114"})
        with tempfile.TemporaryDirectory() as directory,                 patch.object(jlc_http.http.client, "HTTPSConnection", side_effect=AssertionError("network")),                 patch.object(jlc_http, "connect_before", side_effect=AssertionError("network")):
            lookup = part_lookup.PartLookup(DesignSnapshot((header,), "test"), Path(directory),
                                            digikey=part_lookup.DigiKeyLogin("id", "secret"))
            lookup.identify()
            self.assertEqual(lookup.needs_network(), (1, 0))
            lookup.fetch(network=False)
            found = run_heavy(DesignSnapshot((header,), "test"), providers=[lookup.provider()])
        self.assertEqual([item.rule for item in found], ["fit.unverified"])

    def test_every_contactable_host_is_documented(self):
        privacy = (DOCS / "privacy.md").read_text(encoding="utf-8")
        guide = (DOCS / "jlcpcb.md").read_text(encoding="utf-8")
        for host in jlc_http.ALLOWED_HOSTS:
            self.assertIn(host, privacy)
            self.assertIn(host, guide)
        from velatrace import part_lookup
        self.assertEqual({url.split("/")[2] for url in (jlc_catalog.BASE_URL, jlc_live.URL, part_lookup.EASYEDA_URL,
                                                        part_lookup.DIGIKEY_TOKEN_URL, part_lookup.DIGIKEY_SEARCH_URL)},
                         set(jlc_http.ALLOWED_HOSTS))


if __name__ == "__main__":
    unittest.main()
