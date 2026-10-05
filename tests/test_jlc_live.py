"""Live JLCPCB stock/prices: mocked HTTP only. Rate limit, cache, labels, findings, estimate."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from jlc_fixture import install, live_payload
from velatrace import jlc_live, jlc_parts
from velatrace.jlc_http import JlcNetError
from velatrace.models import Component, DesignSnapshot, Pin

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def comp(ref, value, footprint, lcsc=""):
    return Component(ref, value, footprint, (Pin("1", "N" + ref), Pin("2", "GND")),
                     {"LCSC": lcsc} if lcsc else {})


class Web:
    def __init__(self, answers):
        self.answers, self.urls, self.sleeps, self.time = answers, [], [], 0.0

    def fetch(self, url, **kwargs):
        self.urls.append(url)
        self.time += 0.2  # each request takes 200 ms
        assert kwargs["max_bytes"] <= 300_000 and kwargs["timeout"] <= 15
        code = url.rsplit("=", 1)[-1]
        answer = self.answers[code]
        if isinstance(answer, Exception):
            raise answer
        return 200, {}, answer

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.time += seconds

    def refresh(self, config, codes, now=T0, **kwargs):
        return jlc_live.refresh(config, codes, fetch=self.fetch, sleep=self.sleep,
                                clock=lambda: self.time, now=lambda: now, **kwargs)


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def test_one_request_per_code_only_the_code_is_sent_and_rate_limited(self):
        web = Web({"C1": live_payload("C1", stock=7), "C22": live_payload("C22"), "C333": live_payload("C333")})
        result = web.refresh(self.config, ["c22", "C1", "C1", "C333", "not-a-code", "C1; rm"])
        self.assertEqual(web.urls, [jlc_live.URL.format(code) for code in ("C1", "C22", "C333")])
        self.assertTrue(all(url.startswith("https://cart.jlcpcb.com/") for url in web.urls))
        self.assertEqual(web.sleeps, [0.8, 0.8])  # 1 s between request starts
        self.assertEqual((result.asked, result.failed, result.skipped), (3, {}, 0))
        self.assertEqual(result.parts["C1"].stock, 7)
        self.assertEqual(result.parts["C1"].unit_price(10), Decimal("0.04"))

    def test_answers_are_cached_with_their_time_and_reused(self):
        web = Web({"C1": live_payload("C1", stock=7)})
        web.refresh(self.config, ["C1"])
        cached = jlc_live.load_live(self.config)["C1"]
        self.assertEqual(cached.fetched_at, "2026-10-04T12:00:00Z")
        self.assertTrue(cached.label.startswith("live as of 2026-10-0"))
        again = web.refresh(self.config, ["C1"], now=T0 + timedelta(hours=1))
        self.assertEqual((again.asked, len(web.urls), again.parts["C1"].stock), (0, 1, 7))
        web.refresh(self.config, ["C1"], now=T0 + timedelta(hours=7))
        self.assertEqual(len(web.urls), 2)
        jlc_live.clear_live(self.config)
        self.assertEqual(jlc_live.load_live(self.config), {})

    def test_unknown_part_and_bad_answers(self):
        web = Web({"C1": b'{"code":400,"data":null,"message":"Parameter exception."}',
                   "C2": b"<html>blocked</html>", "C3": live_payload("C9"),
                   "C4": live_payload("C4", stock=-5)})
        result = web.refresh(self.config, ["C1", "C2"])
        self.assertFalse(result.parts["C1"].found)
        self.assertIn("C2", result.failed)
        self.assertNotIn("C2", result.parts)
        for code in ("C3", "C4"):  # wrong part echoed, impossible stock
            with self.assertRaises(JlcNetError):
                jlc_live.parse_part(code, web.answers[code], "2026-10-04T12:00:00Z")

    def test_stops_after_repeated_failures_and_caps_per_click(self):
        web = Web({f"C{i}": JlcNetError("cart.jlcpcb.com answered HTTP 403.") for i in range(1, 11)})
        result = web.refresh(self.config, [f"C{i}" for i in range(1, 11)])
        self.assertEqual((result.asked, len(result.failed)), (jlc_live.MAX_FAILURES, 10))
        self.assertIn("403", result.summary)
        many = Web({f"C{i}": live_payload(f"C{i}") for i in range(1, 260)})
        many.sleep = lambda seconds: None
        result = many.refresh(self.config, list(many.answers))
        self.assertEqual((result.asked, result.skipped), (jlc_live.MAX_PARTS, 59))

    def test_corrupt_cache_is_ignored(self):
        path = self.config / jlc_live.CACHE_DIR / jlc_live.CACHE_FILE
        path.parent.mkdir(parents=True)
        path.write_text('{"format": 1, "parts": {"C1": {"stock": "lots"}}}', encoding="utf-8")
        self.assertEqual(jlc_live.load_live(self.config), {})


class PartsTableTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.catalogue = install(self.config)
        self.addCleanup(self.catalogue.close)
        self.snapshot = DesignSnapshot((
            comp("C1", "100n", "Capacitor_SMD:C_0402_1005Metric", "C1608"),
            comp("C2", "100n", "Capacitor_SMD:C_0402_1005Metric", "C1608"),
            comp("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
            comp("R2", "4k7", "Resistor_SMD:R_0402_1005Metric", "C25900"),
            comp("U1", "RP2040", "Package_DFN_QFN:QFN-56", "C2040"),
            comp("R9", "10k", "Resistor_SMD:R_0603_1608Metric", "C25804"),
            comp("H1", "MountingHole", "MountingHole:MountingHole_3.2mm")), "test")

    def live(self, **parts):
        return {code: jlc_live.parse_part(code, payload, "2026-10-04T12:00:00Z")
                for code, payload in parts.items()}

    def test_table_lists_every_line_with_tier_stock_price_and_fee(self):
        lines = {line.refs: line for line in jlc_parts.bom_table(
            self.snapshot, self.catalogue, excluded={"R9"})}
        self.assertEqual(set(lines), {("C1", "C2"), ("R1",), ("R2",), ("U1",), ("H1",)})
        caps = lines[("C1", "C2")]
        self.assertEqual((caps.lcsc, caps.tier, caps.stock, caps.package, caps.fee),
                         ("C1608", "extended", 60000, "0402", True))
        self.assertEqual(caps.unit_price(5), Decimal("0.0120"))     # 10 pcs
        self.assertEqual(caps.unit_price(100), Decimal("0.0080"))   # 200 pcs
        self.assertIn("published 2026-09-26", caps.source)
        self.assertEqual((lines[("R1",)].tier, lines[("R1",)].suggestion), ("", "C25744"))
        self.assertEqual((lines[("R2",)].tier, lines[("R2",)].fee), ("basic", False))

    def test_live_overrides_catalogue_and_is_labelled(self):
        live = self.live(C1608=live_payload("C1608", stock=3, prices=((1, -1, 0.02),)),
                         C2040=b'{"code":400,"data":null}')
        lines = {line.lcsc: line for line in jlc_parts.bom_table(self.snapshot, self.catalogue, live)}
        self.assertEqual((lines["C1608"].stock, lines["C1608"].unit_price(5)), (3, Decimal("0.02")))
        self.assertTrue(lines["C1608"].source.startswith("live as of "))
        self.assertEqual((lines["C2040"].tier, lines["C2040"].stock), ("unlisted", 0))

    def test_stock_findings_and_cost_estimate(self):
        live = self.live(C1608=live_payload("C1608", stock=6, prices=((1, -1, 0.02),)))
        found = jlc_parts.stock_findings(jlc_parts.bom_table(self.snapshot, self.catalogue, live))
        by_rule = {}
        for item in found:
            by_rule.setdefault(item.rule, []).append(item)
        out = by_rule["jlc.out_of_stock"][0]          # C25900 has stock 0 in the catalogue
        self.assertEqual(out.refs, ("R2",))
        self.assertIn("catalogue", out.evidence)
        self.assertIn("Refresh", out.evidence)
        low = by_rule["jlc.low_stock"][0]             # 6 in stock, 2 per board, 5 boards need 10
        self.assertEqual((low.refs, low.severity.value), (("C1", "C2"), "warning"))
        self.assertIn("live as of", low.evidence)
        self.assertIn("Enough for 3 board(s)", low.evidence)
        estimate = by_rule["jlc.cost_estimate"][0]
        self.assertIsNone(estimate.cost_delta)
        # At 5 boards: caps 2 x 0.02 + R2 0.001 + R9 0.002 + U1 0.987, fees 2 x $3 / 5.
        rows = jlc_parts.cost_estimate(jlc_parts.bom_table(self.snapshot, self.catalogue, live))
        self.assertEqual(rows[0].parts_per_board, Decimal("0.04") + Decimal("0.001") + Decimal("0.002")
                         + Decimal("0.987"))
        self.assertEqual((rows[0].fees_per_order, rows[0].per_board), (Decimal("6.00"), Decimal("2.23")))
        self.assertEqual(rows[-1].boards, 100)
        self.assertEqual(rows[2].parts_per_board - rows[0].parts_per_board,
                         Decimal("0.817") - Decimal("0.987"))  # RP2040 price break at 30 pcs
        self.assertIn("$2.23 at 5", estimate.title)

    def test_design_findings_runs_offline_from_disk(self):
        rules = {item.rule for item in jlc_parts.design_findings(self.snapshot, self.config)}
        self.assertTrue({"bom.jlc_assign", "bom.jlc_fee_summary", "jlc.out_of_stock",
                         "jlc.cost_estimate"} <= rules)
        empty = Path(self.directory.name) / "none"
        self.assertNotIn("jlc.cost_estimate",
                         {item.rule for item in jlc_parts.design_findings(self.snapshot, empty)})


if __name__ == "__main__":
    unittest.main()
