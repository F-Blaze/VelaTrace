"""BOM consolidation and JLCPCB Basic-part checks: offline, deterministic, conservative."""
from decimal import Decimal
from pathlib import Path
import unittest

from velatrace.bom import (bom_findings, classify_roles, format_value, lcsc_code, mpn_of,
                           package_of, parse_value, passives)
from velatrace.findings import Severity
from velatrace.models import Component, DesignSnapshot, Pin
from velatrace.parts_db import parse_catalogue

FIXTURE = Path(__file__).parent / "fixtures" / "bom" / "economic-parts.csv"
R0402 = "Resistor_SMD:R_0402_1005Metric"
R0603 = "Resistor_SMD:R_0603_1608Metric"
C0402 = "Capacitor_SMD:C_0402_1005Metric"
C0603 = "Capacitor_SMD:C_0603_1608Metric"
MCU = Component("U1", "MCU", "Package_QFP:LQFP-48", (
    Pin("1", "+3V3", "VDD"), Pin("2", "GND", "VSS"), Pin("3", "/SDA", "PB7"),
    Pin("4", "/SCL", "PB6"), Pin("5", "/BOOT", "BOOT0"), Pin("6", "/NRST", "NRST"),
    Pin("7", "/EN", "EN"), Pin("8", "/FB", "FB"), Pin("9", "/CC1", "CC1"), Pin("10", "/LED", "PA1")))


def two_pin(ref, value, footprint, a, b, **fields):
    return Component(ref, value, footprint, (Pin("1", a), Pin("2", b)), fields)


def snapshot(*parts):
    return DesignSnapshot((MCU,) + parts, "fixture")


def db():
    return parse_catalogue(FIXTURE.read_bytes(), "lrks-economic", fetched_at="2026-09-30T00:00:00Z",
                           min_basic_resistors=1, min_basic_capacitors=1)


def rules(findings, rule):
    return [finding for finding in findings if finding.rule == rule]


class ValueParsingTests(unittest.TestCase):
    def test_equivalent_spellings_parse_to_one_value(self):
        for text in ("4k7", "4.7k", "4K7", "4K70", "4700", "4.7kΩ", "4700R", "4.7kohm"):
            with self.subTest(text=text):
                self.assertEqual(parse_value(text, "resistor").value, Decimal(4700))
        for text in ("100n", "100nF", "0.1u", "0.1uF", "0.1µF", "100NF", ".1uF"):
            with self.subTest(text=text):
                self.assertEqual(parse_value(text, "capacitor").value, Decimal("1e-7"))

    def test_rkm_zero_ohm_and_small_values(self):
        self.assertEqual(parse_value("0R", "resistor").value, 0)
        self.assertEqual(parse_value("0R1", "resistor").value, Decimal("0.1"))
        self.assertEqual(parse_value("R10", "resistor").value, Decimal("0.10"))
        self.assertEqual(parse_value("1n5", "capacitor").value, Decimal("1.5e-9"))
        self.assertEqual(parse_value("2M2", "resistor").value, Decimal("2.2e6"))
        self.assertEqual(parse_value("1m", "resistor").value, Decimal("0.001"))  # milli, not mega
        self.assertEqual(parse_value("10u", "capacitor").value, Decimal("1e-5"))

    def test_rating_suffixes_are_recorded_not_mistaken_for_values(self):
        value = parse_value("0.1uF/50V/X7R", "capacitor")
        self.assertEqual((value.voltage, value.dielectric), (Decimal(50), "X7R"))
        self.assertEqual(parse_value("10k 1%", "resistor").tolerance, Decimal(1))
        self.assertEqual(parse_value("4.7k ±5%", "resistor").tolerance, Decimal(5))
        self.assertEqual(parse_value("10uF 6V3", "capacitor").voltage, Decimal("6.3"))
        self.assertEqual(parse_value("22p NP0", "capacitor").dielectric, "C0G")
        self.assertEqual(parse_value("10k 1/4W", "resistor").power, "1/4W")

    def test_ambiguous_or_unpopulated_values_are_refused(self):
        for text, kind in (("100", "capacitor"), ("1M", "capacitor"), ("DNP", "resistor"),
                           ("10k DNP", "resistor"), ("", "resistor"), ("10uF", "resistor"),
                           ("abc", "resistor"), ("4k7", "inductor")):
            with self.subTest(text=text):
                self.assertIsNone(parse_value(text, kind))

    def test_display_and_packages(self):
        self.assertEqual(format_value(Decimal(4700), "resistor"), "4.7kΩ")
        self.assertEqual(format_value(Decimal("1e-7"), "capacitor"), "100nF")
        self.assertEqual(format_value(Decimal(0), "resistor"), "0Ω")
        self.assertEqual(package_of(R0402), "0402")
        self.assertEqual(package_of("Resistor_SMD:R_0201_0603Metric"), "0201")
        self.assertEqual(package_of("Capacitor_SMD:C_1206_3216Metric_Pad1.33x1.80mm_HandSolder"), "1206")
        self.assertIsNone(package_of("Resistor_THT:R_Axial_DIN0207_L6.3mm"))
        self.assertIsNone(package_of("Capacitor_Tantalum_SMD:CP_EIA-3216-18_Kemet-A"))
        self.assertIsNone(package_of("Resistor_SMD:R_Array_Convex_4x0603"))


class RoleTests(unittest.TestCase):
    def roles(self, *parts):
        snap = snapshot(*parts)
        return {ref: role.role for ref, role in classify_roles(snap, passives(snap)).items()}

    def test_simple_pulls_and_decoupling(self):
        roles = self.roles(two_pin("R1", "4.7k", R0402, "+3V3", "/SDA"),
                           two_pin("R2", "10k", R0402, "/BOOT", "GND"),
                           two_pin("C1", "100n", C0402, "+3V3", "GND"))
        self.assertEqual(roles, {"R1": "pullup", "R2": "pulldown", "C1": "decoupling"})

    def test_timing_divider_feedback_led_and_usb_cc_are_not_pulls(self):
        roles = self.roles(
            two_pin("R1", "10k", R0402, "+3V3", "/NRST"), two_pin("C1", "100n", C0402, "/NRST", "GND"),
            two_pin("R2", "100k", R0402, "+3V3", "/FB"),
            two_pin("R3", "10k", R0402, "+3V3", "/EN"), two_pin("R4", "10k", R0402, "/EN", "GND"),
            two_pin("R5", "5.1k", R0402, "/CC1", "GND"),
            two_pin("R6", "1k", R0402, "+3V3", "/LED"), two_pin("D1", "LED", "LED_SMD:LED_0603",
                                                                 "/LED", "GND"),
            two_pin("R7", "10k 0.1%", R0402, "+3V3", "/SCL"))
        self.assertTrue(all(roles[ref] == "other" for ref in ("R1", "R2", "R3", "R4", "R5", "R6", "R7")))

    def test_pin_names_mark_precision_nodes(self):
        sense = Component("U2", "LDO", "Package_TO_SOT_SMD:SOT-23-5",
                          (Pin("1", "/ADJ_NET", "ADJ"), Pin("2", "GND", "GND")))
        roles = self.roles(sense, two_pin("R1", "10k", R0402, "+3V3", "/ADJ_NET"))
        self.assertEqual(roles["R1"], "other")


class ConsolidationTests(unittest.TestCase):
    def test_same_value_different_spelling_is_normalised(self):
        findings = bom_findings(snapshot(two_pin("C1", "100n", C0402, "+3V3", "GND"),
                                         two_pin("C2", "0.1uF", C0402, "+3V3", "GND"),
                                         two_pin("C3", "100nF", C0402, "+3V3", "GND")))
        [finding] = rules(findings, "bom.value_normalise")
        self.assertEqual(finding.severity, Severity.SAVING)
        self.assertEqual(finding.refs, ("C1", "C2", "C3"))
        self.assertIn("3 BOM lines instead of 1", finding.evidence)

    def test_different_ratings_are_not_normalised(self):
        findings = bom_findings(snapshot(two_pin("C1", "10uF 6V3", C0402, "+3V3", "GND"),
                                         two_pin("C2", "10u 25V", C0402, "+3V3", "GND")))
        self.assertEqual(rules(findings, "bom.value_normalise"), [])

    def test_near_pull_values_merge_without_parts_data(self):
        findings = bom_findings(snapshot(two_pin("R1", "4.7k", R0402, "+3V3", "/SDA"),
                                         two_pin("R2", "4.7k", R0402, "+3V3", "/SCL"),
                                         two_pin("R3", "5.1k", R0402, "+3V3", "/EN"),
                                         two_pin("R4", "4.99k", R0402, "/BOOT", "GND")))
        [finding] = rules(findings, "bom.value_merge")
        self.assertIn("4.7kΩ", finding.title)
        self.assertIn("Change R3, R4 to 4.7kΩ", finding.fix)
        self.assertIsNone(finding.cost_delta)  # no parts data: line count only
        self.assertIn("2 fewer BOM lines", finding.evidence)

    def test_far_values_and_unsafe_roles_never_merge(self):
        findings = bom_findings(snapshot(
            two_pin("R1", "4.7k", R0402, "+3V3", "/SDA"),
            two_pin("R2", "10k", R0402, "+3V3", "/SCL"),        # >20% away
            two_pin("R3", "5.1k", R0402, "+3V3", "/FB"),        # feedback node
            two_pin("R4", "5.1k", R0402, "/CC1", "GND"),        # USB-C Rd is spec-mandated
            two_pin("R5", "4.99k", R0402, "+3V3", "/NRST"),     # RC reset timing
            two_pin("C1", "100n", C0402, "/NRST", "GND")))
        # Only the SDA pull may move (onto the existing 5.1k line); R2-R5 never change.
        for finding in rules(findings, "bom.value_merge"):
            self.assertTrue(finding.fix.startswith("Change R1 to 5.1k"), finding.fix)

    def test_same_value_other_package_consolidates(self):
        findings = bom_findings(snapshot(two_pin("C1", "100n", C0402, "+3V3", "GND"),
                                         two_pin("C2", "100n", C0402, "+3V3", "GND"),
                                         two_pin("C3", "100n", C0603, "+3V3", "GND")))
        [finding] = rules(findings, "bom.package_merge")
        self.assertIn("use 0402", finding.title)
        self.assertIn("C3", finding.fix)

    def test_package_merge_skips_bulk_caps_and_non_movable_roles(self):
        findings = bom_findings(snapshot(
            two_pin("C1", "10u", C0402, "+3V3", "GND"), two_pin("C2", "10u", C0603, "+3V3", "GND"),
            two_pin("R1", "10k", R0402, "/CC1", "GND"), two_pin("R2", "10k", R0603, "+3V3", "/FB")))
        self.assertEqual(rules(findings, "bom.package_merge"), [])
        # A simple pull-up may move into the package its feedback twin already uses.
        findings = bom_findings(snapshot(two_pin("R1", "10k", R0402, "+3V3", "/SDA"),
                                         two_pin("R2", "10k", R0603, "+3V3", "/FB")))
        [finding] = rules(findings, "bom.package_merge")
        self.assertIn("R1 (0402) pullup on /SDA", finding.evidence)
        self.assertNotIn("voltage", finding.fix)


class JlcTests(unittest.TestCase):
    def test_no_parts_data_skips_jlc_checks(self):
        findings = bom_findings(snapshot(two_pin("R1", "10k", R0402, "+3V3", "/SDA", LCSC="C1")))
        self.assertFalse([f for f in findings if f.rule.startswith(("bom.jlc", "bom.lcsc"))])

    def test_extended_lcsc_gets_basic_equivalent_with_fee(self):
        findings = bom_findings(snapshot(two_pin("C1", "100nF 25V", C0402, "+3V3", "GND",
                                                 LCSC="C123456")), db())
        [finding] = rules(findings, "bom.jlc_basic_equivalent")
        self.assertEqual(finding.severity, Severity.SAVING)
        self.assertIn("C307331", finding.title)  # 50V; the 16V Basic part is below the 25V rating
        self.assertEqual(finding.cost_delta, Decimal("-0.60"))
        [summary] = rules(findings, "bom.jlc_fee_summary")
        self.assertIn("$3.00", summary.title)

    def test_basic_and_preferred_lcsc_have_no_fee(self):
        findings = bom_findings(snapshot(two_pin("R1", "10k", R0402, "+3V3", "/SDA", LCSC="C25744"),
                                         two_pin("R2", "510k", R0402, "/BOOT", "GND", LCSC="C11616")),
                                db())
        self.assertFalse([f for f in findings if f.rule.startswith("bom.jlc")])

    def test_dropped_preferred_part_is_extended(self):
        findings = bom_findings(snapshot(two_pin("R1", "49.9k", R0402, "+3V3", "/FB",
                                                 LCSC="C900001")), db())
        self.assertEqual(len(rules(findings, "bom.jlc_fee_summary")), 1)

    def test_lcsc_value_mismatch_is_a_warning(self):
        findings = bom_findings(snapshot(two_pin("R1", "10k", R0402, "+3V3", "/SDA", LCSC="C25804")),
                                db())
        [finding] = rules(findings, "bom.lcsc_mismatch")
        self.assertEqual(finding.severity, Severity.WARNING)

    def test_passive_without_lcsc_matches_by_value_and_package(self):
        findings = bom_findings(snapshot(two_pin("R1", "10k", R0402, "+3V3", "/SDA")), db())
        [finding] = rules(findings, "bom.jlc_assign")
        self.assertIn("C25744", finding.title)

    def test_dielectric_and_voltage_are_never_downgraded(self):
        findings = bom_findings(snapshot(two_pin("C1", "220p C0G", C0402, "/LED", "GND"),
                                         two_pin("C2", "2.2u", C0402, "+12V", "GND")), db())
        self.assertEqual(rules(findings, "bom.jlc_assign"), [])
        summary = rules(findings, "bom.jlc_fee_summary")[0]
        self.assertEqual(summary.refs, ("C1", "C2"))

    def test_unmatched_pull_value_gets_nearest_basic_value(self):
        findings = bom_findings(snapshot(two_pin("R1", "4.99k", R0402, "+3V3", "/SDA")), db())
        [finding] = rules(findings, "bom.jlc_basic_alternative")
        self.assertIn("C25905", finding.title)  # 5.1k is closer than 4.7k
        self.assertEqual(finding.cost_delta, Decimal("-0.60"))

    def test_unmatched_non_pull_value_is_not_changed(self):
        findings = bom_findings(snapshot(two_pin("R1", "4.99k", R0402, "+3V3", "/FB")), db())
        self.assertEqual(rules(findings, "bom.jlc_basic_alternative"), [])

    def test_merge_cost_uses_parts_data_and_board_count(self):
        parts = (two_pin("R1", "4.7k", R0402, "+3V3", "/SDA"), two_pin("R2", "4.7k", R0402, "+3V3", "/SCL"),
                 two_pin("R3", "4.99k", R0402, "+3V3", "/EN"))
        [finding] = rules(bom_findings(snapshot(*parts), db(), boards_per_order=10), "bom.value_merge")
        self.assertEqual(finding.cost_delta, Decimal("-0.30"))
        with self.assertRaises(ValueError):
            bom_findings(snapshot(*parts), boards_per_order=0)


PHY = Component("U2", "PHY", "Package_DFN_QFN:QFN-32", (
    Pin("1", "Net-(U2-EPHY_RTX)", "EPHY_RTX"), Pin("2", "/TACH", "TACH"),
    Pin("3", "Net-(U2-Pad3)", "PA3"), Pin("4", "Net-(U2-Pad4)", "PA4")))
C1206 = "Capacitor_SMD:C_1206_3216Metric"


def with_phy(*parts):
    return DesignSnapshot((MCU, PHY) + parts, "fixture")


class BomPrecisionTests(unittest.TestCase):
    """Bench regressions: ottercast, rs485-moist-sensor, zereader, bitaxe, kicad-demo-video, tokay."""

    def test_bias_reference_nets_are_never_pulls(self):
        snap = with_phy(two_pin("R1", "5.6k", R0402, "GND", "Net-(U2-EPHY_RTX)"),
                        two_pin("R2", "5.6k", R0402, "+3V3", "/TACH"))
        roles = classify_roles(snap, passives(snap))
        self.assertEqual(roles["R1"].role, "other")
        self.assertEqual(roles["R2"].role, "pullup")

    def test_e96_values_need_a_pull_signal_name(self):
        snap = with_phy(two_pin("R1", "6k04", R0402, "GND", "Net-(U2-Pad3)"),  # unknown role
                        two_pin("R2", "4k99", R0402, "+3V3", "/SDA"),          # named I2C pull
                        two_pin("R3", "5.1k", R0402, "GND", "Net-(U2-Pad4)"))  # E24: topology
        roles = classify_roles(snap, passives(snap))
        self.assertEqual(roles["R1"].role, "other")
        self.assertEqual(roles["R2"].role, "pullup")
        self.assertEqual(roles["R3"].role, "pulldown")

    def test_ephy_rtx_6k04_is_not_merged_into_pull_line(self):
        snap = with_phy(two_pin("R1", "5.1k", R0402, "+3V3", "/SDA"),
                        two_pin("R2", "5.1k", R0402, "+3V3", "/SCL"),
                        two_pin("R3", "6k04", R0402, "GND", "Net-(U2-EPHY_RTX)"))
        self.assertEqual(rules(bom_findings(snap), "bom.value_merge"), [])

    def test_tolerance_on_one_member_only_is_incompatible(self):
        self.assertFalse(parse_value("10k", "resistor").compatible(parse_value("10k 0.1%", "resistor")))
        self.assertTrue(parse_value("10k 1%", "resistor").compatible(parse_value("10K 1%", "resistor")))
        findings = bom_findings(snapshot(two_pin("R1", "10k", R0402, "+3V3", "/SDA"),
                                         two_pin("R2", "10k 0.1%", R0402, "+3V3", "/SCL")))
        self.assertEqual(rules(findings, "bom.value_normalise"), [])

    def test_part_number_on_one_member_or_different_mpns_block_normalise(self):
        findings = bom_findings(snapshot(
            two_pin("R1", "10k", R0402, "+3V3", "/SDA"),
            two_pin("R2", "10K", R0402, "+3V3", "/SCL", PartNo="ERJ3RBD1002V")))
        self.assertEqual(rules(findings, "bom.value_normalise"), [])
        findings = bom_findings(snapshot(
            two_pin("R1", "10k", R0402, "+3V3", "/SDA", MPN="RC0402FR-0710KL"),
            two_pin("R2", "10K", R0402, "+3V3", "/SCL", MPN="ERJ-2RKF1002X")))
        self.assertEqual(rules(findings, "bom.value_normalise"), [])

    def test_same_mpn_ignores_conflicting_voltage_text(self):
        mpn = {"Manufacturer PartNo": "CL31B105KBHNFNE"}
        parts = (two_pin("C1", "1uF, 25V", C1206, "+3V3", "GND", **mpn),
                 two_pin("C2", "1uF, 50V", C1206, "/VGH", "GND", **mpn),
                 two_pin("C3", "1uF, 10V", C1206, "/VGL", "GND", **mpn))
        [finding] = rules(bom_findings(snapshot(*parts)), "bom.value_normalise")
        self.assertEqual(finding.refs, ("C1", "C2", "C3"))
        self.assertIn("CL31B105KBHNFNE", finding.evidence)
        generic = tuple(two_pin(p.reference, p.value, C1206, "+3V3", "GND") for p in parts)
        self.assertEqual(rules(bom_findings(snapshot(*generic)), "bom.value_normalise"), [])

    def test_normalise_never_suggests_dropping_a_voltage_rating(self):
        [finding] = rules(bom_findings(snapshot(
            two_pin("C1", "10u", C0402, "+3V3", "GND"), two_pin("C2", "10u", C0402, "+3V3", "GND"),
            two_pin("C3", "10u 25V", C0402, "+3V3", "GND"))), "bom.value_normalise")
        self.assertIn("'10u 25V'", finding.fix)

    def test_pull_can_move_onto_an_existing_non_pull_line(self):
        snap = with_phy(two_pin("R1", "5.6k", R0402, "+3V3", "/TACH"),
                        two_pin("R2", "5.1k", R0402, "/CC1", "GND"),
                        two_pin("R3", "5.1k", R0402, "/CC1", "GND"))
        [finding] = rules(bom_findings(snap), "bom.value_merge")
        self.assertTrue(finding.fix.startswith("Change R1 to 5.1k"), finding.fix)
        self.assertIn("already on the BOM", finding.evidence)
        self.assertEqual(finding.refs, ("R1", "R2", "R3"))

    def test_existing_line_with_other_ratings_or_far_value_is_not_a_target(self):
        for other in ("5.1k 1%", "10k"):
            with self.subTest(other=other):
                snap = with_phy(two_pin("R1", "5.6k", R0402, "+3V3", "/TACH"),
                                two_pin("R2", other, R0402, "/CC1", "GND"))
                self.assertEqual(rules(bom_findings(snap), "bom.value_merge"), [])

    def test_comma_decimals_ohm_and_rkm_notation(self):
        cases = {("4,7uF", "capacitor"): Decimal("4.7e-6"), ("2,2k", "resistor"): Decimal(2200),
                 ("5,6K", "resistor"): Decimal(5600), ("10 kΩ", "resistor"): Decimal(10000),
                 ("10 kΩ", "resistor"): Decimal(10000), ("2R2", "resistor"): Decimal("2.2"),
                 ("4k7Ω", "resistor"): Decimal(4700), ("100 nF 50V", "capacitor"): Decimal("1e-7"),
                 ("0402 10uF", "capacitor"): Decimal("1e-5"),
                 ("4.7 µF", "capacitor"): Decimal("4.7e-6")}
        for (text, kind), value in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_value(text, kind).value, value)
        self.assertEqual(parse_value("100 nF 50V", "capacitor").voltage, Decimal(50))
        self.assertTrue(parse_value("1k 0402 Resistor", "resistor").compatible(
            parse_value("1K 0402", "resistor")))
        for text, kind in (("4,7uF", "resistor"), ("10 V", "capacitor"), ("4k7F", "resistor"),
                           ("1,5", "capacitor")):
            with self.subTest(text=text):
                self.assertIsNone(parse_value(text, kind))

    def test_comma_decimal_spelling_is_normalised(self):
        [finding] = rules(bom_findings(snapshot(
            two_pin("C1", "4,7uF", C0603, "+3V3", "GND"),
            two_pin("C2", "4.7uF", C0603, "+3V3", "GND"))), "bom.value_normalise")
        self.assertEqual(finding.refs, ("C1", "C2"))

    def test_supplier_part_fields(self):
        def comp(**fields):
            return two_pin("C1", "1u", C0402, "+3V3", "GND", **fields)
        self.assertEqual(lcsc_code(comp(SPN1="C52923")), "C52923")
        self.assertEqual(lcsc_code(comp(**{"LCSC Part #": "c1525"})), "C1525")
        self.assertEqual(lcsc_code(comp(JLCPCB_Part="C1525")), "C1525")
        self.assertEqual(lcsc_code(comp(**{"Supplier Part Number": "490-1234-1-ND",
                                           "LCSC": "C1525"})), "C1525")
        self.assertIsNone(lcsc_code(comp(SPN1="296-1234-1-ND")))
        self.assertIsNone(lcsc_code(comp(Description="C1525")))
        self.assertEqual(mpn_of(comp(**{"Manufacturer PartNo": "CL05A105KA5NQNC"})),
                         "CL05A105KA5NQNC")
        self.assertEqual(mpn_of(comp(PARTNO="RC0402FR-135K6L")), "RC0402FR-135K6L")
        self.assertIsNone(mpn_of(comp(MFR="Samsung", PartNo="?")))

    def test_spn1_lcsc_reaches_the_parts_list_checks(self):
        findings = bom_findings(snapshot(
            two_pin("C1", "1u", C0402, "+3V3", "GND", SPN1="C1525")), db())
        self.assertTrue(rules(findings, "bom.lcsc_mismatch"))  # C1525 is a 100nF part

if __name__ == "__main__":
    unittest.main()
