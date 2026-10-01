"""Parts list: explicit download only, integrity-checked cache, offline reads, no network here."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from velatrace import parts_db
from velatrace.parts_db import (PartsDBError, SOURCES, cache_paths, clear_catalogue,
                                download_catalogue, https_get, load_catalogue, parse_catalogue)

FIXTURE = Path(__file__).parent / "fixtures" / "bom" / "economic-parts.csv"
LOW = {"min_basic_resistors": 1, "min_basic_capacitors": 1}
CDFER = ("lcsc,present,subcategory,mfr,package,library_type,preferred,basic,description,attributes\n"
         '25744,1,Chip Resistor - Surface Mount,0402WGF1002TCE,0402,base,0,1,,"{""Resistance"":""10kΩ"",'
         '""Tolerance"":""±1%""}"\n'
         "1525,1,Multilayer Ceramic Capacitors MLCC - SMD/SMT,CL05B104KO5NNNC,0402,base,0,1,"
         "100nF 16V X7R ±10% 0402,{}\n"
         "7420373,1,ESD And Surge Protection (TVS/ESD),H5VL06B,DFN0603-2,expand,1,0,ESD,{}\n"
         "9999,1,Chip Resistor - Surface Mount,X,0402,expand,0,0,1kΩ,{}\n")


def fetch_fixture(url, max_bytes, timeout):
    assert url == SOURCES["lrks-economic"].url
    return FIXTURE.read_bytes()


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        patcher = patch.object(parts_db, "MIN_DOWNLOAD_BYTES", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def download(self, fetch=fetch_fixture, **kwargs):
        return download_catalogue(self.config, fetch=fetch, now=datetime(2026, 9, 30, tzinfo=timezone.utc),
                                  **{**LOW, **kwargs})

    def test_lookup_basic_preferred_and_extended(self):
        db = parse_catalogue(FIXTURE.read_bytes(), "lrks-economic", **LOW)
        self.assertEqual(db.tier("C25744"), "basic")
        self.assertEqual(db.tier("c11616"), "preferred")
        self.assertIsNone(db.tier("C900001"))  # dropped from the Economic list
        self.assertIsNone(db.tier("C123456"))  # never listed: Extended
        self.assertEqual(db.find_mpn("cl05b104ko5nnnc").lcsc, "C1525")
        self.assertEqual(db.part("C1525").value, Decimal("1e-7"))
        self.assertEqual([p.lcsc for p in db.equivalents("capacitor", Decimal("1e-7"), "0402")],
                         ["C307331", "C1525"])
        self.assertEqual([p.lcsc for p in db.equivalents("capacitor", Decimal("1e-7"), "0402",
                                                         min_voltage=Decimal(25))], ["C307331"])
        self.assertEqual(db.equivalents("capacitor", Decimal("2.2e-10"), "0402",
                                        dielectric="C0G", allowed_dielectrics={"C0G"}), [])

    def test_cdfer_format_is_supported(self):
        db = parse_catalogue(CDFER.encode(), "cdfer-basic-preferred", **LOW)
        self.assertEqual(db.part("C25744").value, Decimal(10000))
        self.assertEqual(db.tier("C7420373"), "preferred")
        self.assertIsNone(db.tier("C9999"))

    def test_incomplete_upstream_list_is_refused(self):
        with self.assertRaisesRegex(PartsDBError, "incomplete"):
            parse_catalogue(FIXTURE.read_bytes(), "lrks-economic")
        with self.assertRaisesRegex(PartsDBError, "incomplete"):
            self.download(min_basic_resistors=1000)
        self.assertIsNone(load_catalogue(self.config))  # nothing cached

    def test_download_caches_and_loads_offline(self):
        self.assertIsNone(load_catalogue(self.config))
        downloaded = self.download()
        data_path, meta_path = cache_paths(self.config)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta["bytes"], data_path.stat().st_size)
        self.assertEqual(meta["fetched_at"], "2026-09-30T00:00:00Z")

        def no_network(*args, **kwargs):
            raise AssertionError("network used")
        with patch.object(parts_db, "https_get", no_network), \
                patch("velatrace.parts_db.http.client.HTTPSConnection", no_network):
            loaded = load_catalogue(self.config)
        self.assertEqual(len(loaded), len(downloaded))
        self.assertEqual(loaded.sha256, downloaded.sha256)
        clear_catalogue(self.config)
        self.assertIsNone(load_catalogue(self.config))

    def test_tampered_or_partial_cache_is_rejected(self):
        self.download()
        data_path, meta_path = cache_paths(self.config)
        data_path.write_bytes(data_path.read_bytes().replace(b"expand,0", b"base,0 "))
        with self.assertRaisesRegex(PartsDBError, "integrity"):
            load_catalogue(self.config)
        self.download()
        meta_path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(PartsDBError, "unreadable"):
            load_catalogue(self.config)
        meta_path.unlink()
        with self.assertRaisesRegex(PartsDBError, "unreadable"):
            load_catalogue(self.config)

    def test_bad_payloads_are_not_cached(self):
        for payload, message in ((b"not,a,parts,list\n1,2,3,4\n", "columns"),
                                 (b"\xff\xfe\x00", "UTF-8"),
                                 (FIXTURE.read_bytes().replace(b"C25744", b"X25744"), "LCSC")):
            with self.subTest(message=message), self.assertRaisesRegex(PartsDBError, message):
                self.download(fetch=lambda *args, payload=payload: payload)
        self.assertIsNone(load_catalogue(self.config))
        with patch.object(parts_db, "MIN_DOWNLOAD_BYTES", 10_000), \
                self.assertRaisesRegex(PartsDBError, "implausibly"):
            self.download()
        with self.assertRaisesRegex(PartsDBError, "Unknown"):
            download_catalogue(self.config, "octopart", fetch=fetch_fixture)


class FakeResponse:
    def __init__(self, status, body=b"", headers=None):
        self.status, self.body, self.headers = status, body, headers or {}

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, size):
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk

    def close(self):
        pass


def fake_connection(response, requests):
    class Connection:
        sock = None

        def __init__(self, host, port, **kwargs):
            requests.append((host, port, kwargs.get("context") is not None))

        def request(self, method, path, headers):
            requests.append((method, path, sorted(headers)))

        def getresponse(self):
            return response

        def close(self):
            pass
    return Connection


class HttpsGetTests(unittest.TestCase):
    url = SOURCES["lrks-economic"].url

    def get(self, response):
        requests = []
        with patch("velatrace.parts_db.http.client.HTTPSConnection", fake_connection(response, requests)):
            return https_get(self.url, 1000, 5), requests

    def test_plain_get_with_verified_tls_and_no_board_data(self):
        body, requests = self.get(FakeResponse(200, b"x" * 10, {"Content-Length": "10"}))
        self.assertEqual(body, b"x" * 10)
        self.assertEqual(requests[0], ("lrks.github.io", 443, True))
        self.assertEqual(requests[1][:2], ("GET", "/jlcpcb-economic-parts/economic-parts.csv"))
        self.assertEqual(requests[1][2], ["Accept", "Accept-Encoding", "User-Agent"])

    def test_redirects_oversize_truncation_and_foreign_urls_are_refused(self):
        for response, message in (
                (FakeResponse(302, headers={"Location": "https://evil.example/x"}), "redirect"),
                (FakeResponse(404), "HTTP 404"),
                (FakeResponse(200, b"x", {"Content-Length": "5000"}), "size limit"),
                (FakeResponse(200, b"x" * 2000), "size limit"),
                (FakeResponse(200, b"x" * 5, {"Content-Length": "10"}), "truncated")):
            with self.subTest(message=message), self.assertRaisesRegex(PartsDBError, message):
                self.get(response)
        for url in ("http://lrks.github.io/jlcpcb-economic-parts/economic-parts.csv",
                    "https://example.com/economic-parts.csv"):
            with self.subTest(url=url), self.assertRaisesRegex(PartsDBError, "built-in"):
                https_get(url, 1000, 5)


if __name__ == "__main__":
    unittest.main()
