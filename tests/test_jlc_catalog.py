"""Full JLCPCB catalogue: explicit resumable download, verification, offline search. No network."""
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from jlc_fixture import ROWS, Server, install
from velatrace import jlc_catalog, jlc_http
from velatrace.bom import bom_findings
from velatrace.jlc_http import JlcNetError
from velatrace.models import Component, DesignSnapshot, Pin
from velatrace.parts_db import PartsDBError

LOW = {"min_basic_resistors": 1, "min_basic_capacitors": 1}


def passive(ref, value, footprint, lcsc=""):
    return Component(ref, value, footprint, (Pin("1", "A" + ref), Pin("2", "GND")),
                     {"LCSC": lcsc} if lcsc else {})


class Base(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.open = []
        self.addCleanup(lambda: [item.close() for item in self.open])

    def catalogue(self, rows=ROWS):
        self.open.append(install(self.config, rows))
        return self.open[-1]


class DownloadTests(Base):
    def test_size_is_known_before_anything_is_downloaded(self):
        server = Server(self.config / "up.db")
        info = jlc_catalog.remote_info(server.fetch)
        self.assertEqual(info.total, len(server.blob))
        self.assertGreater(len(info.chunks), 2)
        self.assertEqual(info.published, "2026-09-26T10:41:07Z")
        self.assertEqual({method for method, name, _ in server.requests if "zip" in name}, {"HEAD"})

    def test_download_verifies_and_loads_offline(self):
        server = Server(self.config / "up.db")
        seen = []
        meta = jlc_catalog.download(self.config, fetch=server.fetch, **LOW,
                                    progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(seen[-1], (len(server.blob), len(server.blob)))
        self.assertEqual(meta["counts"]["parts"], len(ROWS))
        self.assertFalse((self.config / jlc_catalog.CACHE_DIR / "download").exists())
        catalogue = jlc_catalog.load(self.config)
        self.open.append(catalogue)
        self.assertEqual(catalogue.tier("C25744"), "basic")
        self.assertIn("published 2026-09-26", catalogue.label)

    def test_interrupted_download_resumes_with_range(self):
        server = Server(self.config / "up.db")
        server.fail_after = 1000
        with self.assertRaises(JlcNetError):
            jlc_catalog.download(self.config, fetch=server.fetch, **LOW)
        self.assertIsNone(jlc_catalog.load(self.config))
        server.fail_after = None
        server.requests.clear()
        jlc_catalog.download(self.config, fetch=server.fetch, **LOW)
        ranged = [headers["Range"] for method, _, headers in server.requests if "Range" in headers]
        self.assertEqual(len(ranged), 1)  # only the cut-off chunk continues; finished ones are skipped
        self.assertEqual(len([1 for m, n, _ in server.requests if m == "GET" and "zip" in n]),
                         len(server.files) - 2)
        self.open.append(jlc_catalog.load(self.config))
        self.assertEqual(len(self.open[-1].search("10kΩ")), 2)

    def test_new_upstream_build_discards_partial_files(self):
        server = Server(self.config / "up.db")
        server.fail_after = 1000
        with self.assertRaises(JlcNetError):
            jlc_catalog.download(self.config, fetch=server.fetch, **LOW)
        newer = Server(self.config / "up.db", rows=ROWS[:-1], published="Sun, 27 Sep 2026 10:00:00 GMT")
        jlc_catalog.download(self.config, fetch=newer.fetch, **LOW)
        self.assertFalse(any("Range" in headers for _, _, headers in newer.requests))

    def test_corrupt_archive_is_refused_and_old_catalogue_kept(self):
        self.catalogue()
        self.open.pop().close()
        server = Server(self.config / "up.db")
        name = jlc_catalog.CHUNK_FILE.format(2)
        server.files[name] = bytes(len(server.files[name]))
        with self.assertRaisesRegex(PartsDBError, "corrupt|expected format"):
            jlc_catalog.download(self.config, fetch=server.fetch, **LOW)
        self.open.append(jlc_catalog.load(self.config))
        self.assertEqual(self.open[-1].tier("C2040"), "extended")

    def test_incomplete_catalogue_is_refused(self):
        server = Server(self.config / "up.db", rows=ROWS[-2:])
        with self.assertRaisesRegex(PartsDBError, "incomplete"):
            jlc_catalog.download(self.config, fetch=server.fetch)
        self.assertIsNone(jlc_catalog.load(self.config))

    def test_tampered_file_fails_integrity_check(self):
        self.catalogue()
        self.open.pop().close()
        path = jlc_catalog.cache_paths(self.config)[0]
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
        with self.assertRaisesRegex(PartsDBError, "integrity"):
            jlc_catalog.load(self.config)
        path.write_bytes(data[:-10])
        with self.assertRaisesRegex(PartsDBError, "wrong size"):
            jlc_catalog.load(self.config)
        jlc_catalog.clear(self.config)
        self.assertIsNone(jlc_catalog.load(self.config))

    def test_only_fixed_https_hosts_can_be_contacted(self):
        for url in ("http://bouni.github.io/x", "https://example.com/x", "https://bouni.github.io:8443/x",
                    "https://user@cart.jlcpcb.com/x"):
            with self.assertRaises(JlcNetError):
                jlc_http.request(url)
        with self.assertRaises(JlcNetError):
            jlc_http.request("https://cart.jlcpcb.com/x", method="POST")


class SearchTests(Base):
    def test_lookup_by_code_and_mpn_includes_extended(self):
        catalogue = self.catalogue()
        self.assertEqual(catalogue.tier("c2040"), "extended")
        self.assertIsNone(catalogue.part("C204"))       # substring of C2040 is not a match
        self.assertIsNone(catalogue.part("C99999999"))
        self.assertEqual(catalogue.find_mpn("rp2040").lcsc, "C2040")
        self.assertEqual(catalogue.find_mpn("stm32f103c8t6").tier, "preferred")
        part = catalogue.part("C1608")
        self.assertEqual((part.kind, part.value, part.package, part.stock),
                         ("capacitor", Decimal("1e-7"), "0402", 60000))
        self.assertEqual([part.unit_price(q) for q in (1, 99, 100, 5000)],
                         [Decimal("0.0120"), Decimal("0.0120"), Decimal("0.0080"), Decimal("0.0050")])

    def test_search_ranks_basic_first_and_filters(self):
        catalogue = self.catalogue()
        self.assertEqual([p.lcsc for p in catalogue.search("100nF 0402")], ["C1525", "C307331", "C1608"])
        self.assertEqual([p.lcsc for p in catalogue.search("100nF", tiers=("extended",))], ["C1608"])
        self.assertEqual([p.lcsc for p in catalogue.search("4.7kΩ")], ["C25900"])
        self.assertEqual(catalogue.search("4.7kΩ", in_stock=True), [])
        self.assertEqual([p.lcsc for p in catalogue.search("C375503")], ["C375503"])
        self.assertEqual([p.lcsc for p in catalogue.search("1kΩ", package="0603",
                                                           exact=("resistor", Decimal(1000), "0603"))],
                         ["C21190", "C375503"])
        self.assertEqual([p.lcsc for p in catalogue.search("microcontrollers M3")], ["C8734"])
        self.assertEqual(catalogue.search('"; DROP TABLE parts; --'), [])
        self.assertEqual(catalogue.search("ab"), [])

    def test_equivalents_skip_out_of_stock_parts(self):
        catalogue = self.catalogue()
        self.assertEqual(catalogue.equivalents("resistor", Decimal(4700), "0402"), [])
        self.assertEqual([p.lcsc for p in catalogue.equivalents("capacitor", Decimal("1e-7"), "0402")],
                         ["C307331", "C1525"])

    def test_best_parts_db_falls_back(self):
        self.assertIsNone(jlc_catalog.best_parts_db(self.config))
        self.open.append(install(self.config))
        best = jlc_catalog.best_parts_db(self.config)
        self.open.append(best)
        self.assertIsInstance(best, jlc_catalog.JlcCatalog)


class BomWithCatalogueTests(Base):
    def findings(self, components):
        snapshot = DesignSnapshot(tuple(components), "test")
        return {f.rule: f for f in bom_findings(snapshot, self.catalogue())}

    def test_extended_part_with_wrong_value_is_a_mismatch(self):
        found = self.findings([passive(f"R{i}", "100k", "Resistor_SMD:R_0603_1608Metric", "C375503")
                               for i in range(1, 4)])
        self.assertIn("1kΩ", found["bom.lcsc_mismatch"].evidence)
        self.assertEqual(found["bom.lcsc_mismatch"].refs, ("R1", "R2", "R3"))

    def test_basic_part_with_wrong_value_is_a_mismatch(self):
        found = self.findings([passive("C1", "10u", "Capacitor_SMD:C_0402_1005Metric", "C52923")])
        self.assertIn("1uF", found["bom.lcsc_mismatch"].evidence)
        self.assertNotIn("bom.jlc_fee_summary", found)

    def test_extended_to_basic_uses_fee_and_real_price_breaks(self):
        found = self.findings([passive("C1", "100n", "Capacitor_SMD:C_0402_1005Metric", "C1608"),
                               passive("C2", "100n", "Capacitor_SMD:C_0402_1005Metric", "C1608"),
                               Component("U1", "RP2040", "QFN", (Pin("1", "AC1"),), {"LCSC": "C2040"})])
        swap = found["bom.jlc_basic_equivalent"]
        self.assertIn("C307331", swap.title)
        # 5 boards x 2 parts = 10 pcs: Extended $0.0120 -> Basic $0.009 each; fee $3 / 5 boards.
        self.assertEqual(swap.cost_delta, Decimal("-0.60") + (Decimal("0.009") - Decimal("0.0120")) * 2)
        self.assertIn("$0.0120 → $0.009", swap.evidence)
        self.assertIn("2 Extended BOM line(s)", found["bom.jlc_fee_summary"].title)
        self.assertIn("published 2026-09-26", found["bom.jlc_fee_summary"].evidence)

    def test_preferred_part_has_no_fee_and_unassigned_line_gets_a_part(self):
        found = self.findings([Component("U1", "STM32", "LQFP", (Pin("1", "AR1"),), {"LCSC": "C8734"}),
                               passive("R1", "10k", "Resistor_SMD:R_0402_1005Metric")])
        self.assertNotIn("bom.jlc_fee_summary", found)
        self.assertIn("C25744", found["bom.jlc_assign"].title)


if __name__ == "__main__":
    unittest.main()
