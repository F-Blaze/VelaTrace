"""Part-consistency rules: each fires every time the mistake is on the board and stays quiet otherwise."""
import dataclasses
from decimal import Decimal

import pytest

from test_audit_rules import design, part, rules
from velatrace.findings import Severity
from velatrace.rules_parts import _E96, _E_SERIES, _nearest_e96


def found(prefix, *parts):
    return [(f.rule, f.refs) for f in rules(design(*parts), prefix)]


def passive(ref, value, footprint, **fields):
    return part(ref, value, footprint, ("1", "A"), ("2", "B"), fields=fields)


def r(ref="R1", value="10k", footprint="Resistor_SMD:R_0402_1005Metric", **fields):
    return passive(ref, value, footprint, **fields)


def c(ref="C1", value="100nF", footprint="Capacitor_SMD:C_0402_1005Metric", **fields):
    return passive(ref, value, footprint, **fields)


def dnp(comp):
    return dataclasses.replace(comp, dnp=True)


# --- part.mpn_value_mismatch ------------------------------------------------------------------

MISMATCH = "part.mpn_value_mismatch"


@pytest.mark.parametrize("value,mpn", [
    ("100k", "RC0402FR-07100RL"),      # real board: R19
    ("2k2", "RK73H1ETTP1001F"),        # real board: R707
    ("4.7k", "RC0402FR-0710KL"),
    ("1k", "RC0603FR-074K7L"),
    ("1k", "RK73B1ETTP103J"),
    ("1k", "0603WAF1002T5E"),
    ("10k", "0603WAF4702T5E"),
    ("10k", "ERJ-3EKF1001V"),
    ("22k", "ERJ-2GEJ103X"),
])
def test_resistor_value_disagreeing_with_mpn_fires(value, mpn):
    comp = r(value=value, MPN=mpn)
    hits = rules(design(comp), MISMATCH)
    assert [(f.rule, f.severity, f.refs) for f in hits] == [(MISMATCH, Severity.WARNING, ("R1",))]
    assert "MPN" in hits[0].evidence and mpn in hits[0].evidence


@pytest.mark.parametrize("value,mpn,field", [
    ("13p", "MC0603N330J500CT", "PartNo"),   # real board: crystal cap
    ("10nF", "0805B104K500NT", "Manufacturer_Part_Number"),
    ("1uF", "CL10A106KP8NNNC", "MPN"),
    ("1uF", "GRM188R71H104KA93D", "Manufacturer Part Number"),
    ("1uF", "CC0603KRX7R9BB104", "MPN"),
    ("22pF", "CL05C1R5CB5NNNC", "MPN"),
    ("10u", "GRM21BR61E475KA12L", "MPN"),
])
def test_capacitor_value_disagreeing_with_mpn_fires(value, mpn, field):
    assert found(MISMATCH, c(value=value, **{field: mpn})) == [(MISMATCH, ("C1",))]


def test_description_value_disagreement_fires():
    assert found(MISMATCH, r(value="10k", Description="Resistor 1K 1% 0402 1/16W")) == [(MISMATCH, ("R1",))]
    assert found(MISMATCH, c(value="1uF", Description="Capacitor 100nF 50V X7R")) == [(MISMATCH, ("C1",))]


@pytest.mark.parametrize("value,mpn", [
    ("100", "RC0402FR-07100RL"), ("100R", "RC0402FR-07100RL"), ("10k", "RC0402FR-0710KL"),
    ("4k7", "RC0402FR-074K7L"), ("4.7k 1%", "RC0603FR-074K7L"), ("1M", "RC0402FR-071ML"),
    ("1k", "RK73H1ETTP1001F"), ("1k", "RK73B1ETTP102J"), ("49.9", "RK73H1ETTP49R9F"),
    ("10k", "0603WAF1002T5E"), ("47", "0603WAF47R0T5E"), ("10k", "ERJ-3EKF1002V"),
    ("10k", "ERJ-2GEJ103X"),
])
def test_resistor_matching_mpn_is_quiet(value, mpn):
    assert found(MISMATCH, r(value=value, MPN=mpn)) == []


@pytest.mark.parametrize("value,mpn", [
    ("10uF", "CL10A106KP8NNNC"), ("10uF/16V", "CL10A106KP8NNNC"), ("10u", "CL10A106KP8NNNC"),
    ("1.5pF", "CL05C1R5CB5NNNC"), ("100n 0402", "GRM188R71H104KA93D"), ("100nF", "CC0603KRX7R9BB104"),
    ("33pF", "MC0603N330J500CT"), ("33p", "0603N330J500CT"), ("0.1uF", "0805B104K500NT"),
])
def test_capacitor_matching_mpn_is_quiet(value, mpn):
    assert found(MISMATCH, c(value=value, MPN=mpn)) == []


@pytest.mark.parametrize("mpn", ["XYZ-123", "RC0402FR-07100RLX", "RK73H1ETTP100", "ABC1002", "~", "",
                                 "GRM188R71H104", "CL10A106", "RC0402FR-07"])
def test_unknown_or_truncated_mpn_is_quiet(mpn):
    assert found(MISMATCH, r(value="2k2", MPN=mpn)) == []
    assert found(MISMATCH, c(value="2.2uF", MPN=mpn)) == []


def test_mpn_of_the_other_kind_is_quiet():
    assert found(MISMATCH, r(value="2k2", MPN="GRM188R71H104KA93D")) == []
    assert found(MISMATCH, c(value="2.2uF", MPN="RC0402FR-07100RL")) == []


def test_mismatch_quiet_for_dnp_unparsable_value_and_vague_description():
    assert found(MISMATCH, dnp(r(value="100k", MPN="RC0402FR-07100RL"))) == []
    assert found(MISMATCH, r(value="RC0402FR-07100RL", MPN="RC0402FR-07100RL")) == []
    assert found(MISMATCH, r(value="", MPN="RC0402FR-07100RL")) == []
    assert found(MISMATCH, c(value="1uF", Description="Unpolarized capacitor")) == []
    assert found(MISMATCH, c(value="1uF", Description="CAP CER 100nF 16V")) == []
    assert found(MISMATCH, r(value="1k", Description="Resistor 1K 1% 0402")) == []
    assert found(MISMATCH, c(value="1uF", Description="Resistor 100K")) == []  # wrong kind word: not our call


# --- part.footprint_kind_mismatch -------------------------------------------------------------

FP = "part.footprint_kind_mismatch"


@pytest.mark.parametrize("footprint", ["Resistor_SMD:R_0402_1005Metric", "Resistor_THT:R_Axial_DIN0207", "R_0402",
                                       "Resistor_SMD:Whatever"])
def test_capacitor_on_resistor_footprint_fires(footprint):
    hits = rules(design(c("C1", footprint=footprint), c("C2", footprint=footprint)), FP)
    assert [(f.rule, f.severity, f.refs) for f in hits] == [(FP, Severity.INFO, ("C1", "C2"))]


@pytest.mark.parametrize("footprint", ["Capacitor_SMD:C_0603_1608Metric", "Capacitor_THT:CP_Radial_D5.0mm_P2.00mm",
                                       "C_0402", "CP_Elec_6.3x5.4", "Capacitor_SMD:Anything"])
def test_resistor_on_capacitor_footprint_fires(footprint):
    assert found(FP, r("R5", footprint=footprint)) == [(FP, ("R5",))]


def test_both_directions_make_one_finding_per_kind():
    hits = rules(design(c("C1", footprint="R_0402"), r("R1", footprint="C_0402")), FP)
    assert sorted(f.refs for f in hits) == [("C1",), ("R1",)]


@pytest.mark.parametrize("comp", [
    c("C1"), r("R1"), c("C2", footprint="Capacitor_SMD:CP_Elec_4x5.4"), c("C3", footprint="Capacitor_THT:C_Disc_D3.0mm"),
    c("C4", footprint="Package_SO:SOIC-8"), c("C5", footprint="Custom:0402"), r("R2", footprint="Custom:0402"),
    c("C6", footprint="MyLib:Chip_0402"), r("R3", footprint=""),
    passive("L1", "10uH", "Resistor_SMD:R_0402"), passive("FB1", "BLM", "Capacitor_SMD:C_0402"),
])
def test_correct_or_generic_footprint_is_quiet(comp):
    assert found(FP, comp) == []


# --- part.polarized_symbol --------------------------------------------------------------------

POLAR = "part.polarized_symbol"


@pytest.mark.parametrize("lib_id", ["Device:C", "Device:C_Small", "C", "Device:C_US"])
def test_tantalum_mpn_on_non_polar_symbol_fires(lib_id):
    hits = rules(design(c(value="10uF", lib_id=lib_id, MPN="TAP106K016SCS")), POLAR)
    assert [(f.rule, f.severity, f.refs) for f in hits] == [(POLAR, Severity.WARNING, ("C1",))]


@pytest.mark.parametrize("mpn", ["TAJA106K016RNJ", "TAP106K016SCS", "T491B106K016AT", "T495D107M010ATE100",
                                 "293D106X9016B2TE3", "F931A106MAA"])
def test_each_tantalum_series_fires(mpn):
    assert found(POLAR, c(lib_id="Device:C", MPN=mpn)) == [(POLAR, ("C1",))]


@pytest.mark.parametrize("fields", [{"Description": "Tantalum capacitor 10uF"}, {"Description": "Aluminium electrolytic 100uF"},
                                    {"Description": "Aluminum Electrolytic Capacitor"}, {"MPN": "Electrolytic 47uF"}])
def test_polarized_words_fire(fields):
    assert found(POLAR, c(lib_id="Device:C", **fields)) == [(POLAR, ("C1",))]


def test_polarized_footprint_fires():
    assert found(POLAR, c(footprint="Capacitor_SMD:CP_Elec_6.3x5.4", lib_id="Device:C")) == [(POLAR, ("C1",))]
    assert found(POLAR, c(footprint="Capacitor_Tantalum_SMD:CP_EIA-3528-21_Kemet-B", lib_id="Device:C_Small")) == [
        (POLAR, ("C1",))]


@pytest.mark.parametrize("comp", [
    c(value="220uF", lib_id="Device:C"),                                             # ceramic 220 uF exists
    c(value="220uF", lib_id="Device:C", MPN="GRM32ER60J227ME05L"),
    c(lib_id="Device:C_Polarized", MPN="TAP106K016SCS"),
    c(lib_id="Device:CP", MPN="TAP106K016SCS"),
    c(lib_id="Device:CP_Small", footprint="Capacitor_SMD:CP_Elec_4x5.4"),
    c(MPN="TAP106K016SCS"),                                                          # PCB source: no lib_id
    c(lib_id="Device:C", MPN="TPS62160"),                                            # TI regulator, not a tantalum part
    c(lib_id="Device:C", Description="Unpolarized capacitor"),
    c(lib_id="Device:C", Description="Non-polar aluminium electrolytic"),
    c(lib_id="Device:C", Description="Bipolar electrolytic"),
    c(lib_id="Device:C", footprint="Capacitor_SMD:C_0805_2012Metric", MPN="GRM21BR61E106KA73L"),
    r("R1", lib_id="Device:R", MPN="TAP106K016SCS"),
])
def test_polarized_symbol_quiet_on_correct_parts(comp):
    assert found(POLAR, comp) == []


# --- part.diode_type_mismatch -----------------------------------------------------------------

DIODE = "part.diode_type_mismatch"


def diode(value, lib_id, ref="D1"):
    return passive(ref, value, "Diode_SMD:D_SOD-123", lib_id=lib_id)


@pytest.mark.parametrize("value", ["1N4007", "1N4001", "1N4148", "1N4148W", "1N5408", "BAV99", "US1M", "S1J", "M7",
                                   "1n4007", "1N4007-T", "US1B-13-F"])
@pytest.mark.parametrize("lib_id", ["Device:D_Schottky", "Device:D_Schottky_Small"])
def test_schottky_symbol_on_silicon_part_fires(value, lib_id):
    hits = rules(design(diode(value, lib_id)), DIODE)
    assert [(f.rule, f.severity, f.refs) for f in hits] == [(DIODE, Severity.WARNING, ("D1",))]


@pytest.mark.parametrize("value", ["1N4007", "1N4148", "SS14", "SS34", "1N5819", "B5819", "1N5822", "1N4148W"])
def test_zener_symbol_on_non_zener_part_fires(value):
    assert found(DIODE, diode(value, "Device:D_Zener")) == [(DIODE, ("D1",))]


@pytest.mark.parametrize("comp", [
    diode("SS14", "Device:D_Schottky"), diode("1N5819", "Device:D_Schottky"), diode("BAT54", "Device:D_Schottky"),
    diode("1N4007", "Device:D"), diode("1N4148", "Device:D_Small"), diode("BZX84C5V1", "Device:D_Zener"),
    diode("1N4733A", "Device:D_Zener"), diode("5V1", "Device:D_Zener"), diode("MBR0530", "Device:D_Schottky"),
    diode("S1", "Device:D_Schottky"),
    diode("1N4007", ""), passive("D2", "1N4007", "Diode_SMD:D_SOD-123"),
])
def test_diode_type_quiet_on_matching_or_unknown_parts(comp):
    assert found(DIODE, comp) == []


# --- value.nonstandard ------------------------------------------------------------------------

NONSTD = "value.nonstandard"


def test_series_tables():
    assert len(_E96) == 96 and {str(v) for v in (_E96[0], _E96[1], _E96[-1])} == {"1.00", "1.02", "9.76"}
    assert {Decimal(x) for x in ("1.02", "4.99", "2.74", "3.32", "6.81", "2.4", "1.01")} <= _E_SERIES
    assert not {Decimal(x) for x in ("2.73", "3.75", "2.5")} & _E_SERIES
    assert _nearest_e96(Decimal("2.73")) == Decimal("2.74")


@pytest.mark.parametrize("value", ["2.73K", "375", "25k", "2k73", "27.3k", "2.5k", "47.1", "9.99k"])
def test_nonstandard_resistor_fires(value):
    hits = rules(design(r("R1", value=value), r("R2", value=value)), NONSTD)
    assert [(f.rule, f.severity, f.refs) for f in hits] == [(NONSTD, Severity.INFO, ("R1", "R2"))]


def test_groups_per_value_and_names_the_nearest_e96():
    hits = rules(design(r("R1", value="2.73k"), r("R2", value="2.73k"), r("R3", value="375"), r("R4", value="4.99k")),
                 NONSTD)
    assert sorted(f.refs for f in hits) == [("R1", "R2"), ("R3",)]
    assert "2.74" in next(f for f in hits if f.refs == ("R1", "R2")).evidence


@pytest.mark.parametrize("value", ["4.99k", "2.74k", "10.2k", "1.00k", "100", "4k7", "10k", "1M", "2k", "5k", "0",
                                   "0R", "3.3", "1.1k", "9.76M", "0.01", "20M", "100k 1%", "49.9", "100R", "10", "TBD"])
def test_standard_or_out_of_scope_value_is_quiet(value):
    assert found(NONSTD, r("R1", value=value)) == []


def test_nonstandard_quiet_for_dnp_non_resistors_and_orderable_mpn():
    assert found(NONSTD, dnp(r("R1", value="2.73k"))) == []
    assert found(NONSTD, c(value="2.73k")) == []
    assert found(NONSTD, r("R1", value="2.5k", MPN="RK73H1ETTP2501F")) == []  # orderable as given
    assert found(NONSTD, r("R1", value="2.5k", MPN="RK73H1ETTP1001F")) == [(NONSTD, ("R1",))]
