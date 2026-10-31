"""High-speed pair timing rules: delay mismatch and via/layer mismatch inside a differential pair."""
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from velatrace.audit_rules import check_pair_skew, check_pair_vias, run_heavy
from velatrace.copper import BoardCopper, Segment, Via, read_copper_file
from velatrace.findings import Severity
from velatrace.models import Component, DesignSnapshot, Pin

FIXTURE = Path(__file__).parent / "fixtures" / "copper" / "hs_board.kicad_pcb"
BENCH = Path(os.environ.get("VELATRACE_BENCH", Path.home() / "Documents" / "VelaTrace-bench"))
LAYERS = ("F.Cu", "B.Cu")


def track(net, length, layer="F.Cu", y=0.0):
    return Segment(net, layer, (0.0, y), (length, y), 0.2, length)


def via(net, x=0.0):
    return Via(net, (x, 0.0), LAYERS)


def board(*items):
    segments = tuple(item for item in items if isinstance(item, Segment))
    vias = tuple(item for item in items if isinstance(item, Via))
    copper = BoardCopper(LAYERS, 1.6, segments, vias, (), frozenset(), {}, {})
    return DesignSnapshot((), "fixture", copper=copper)


def ids(snapshot):
    return sorted(item.rule for item in check_pair_skew(snapshot) + check_pair_vias(snapshot))


def test_long_detour_on_one_line_is_reported_with_lengths_and_estimate():
    (finding,) = check_pair_skew(board(track("USB_D+", 30), track("USB_D-", 5, y=1)))
    assert finding.rule == "hs.pair.skew" and finding.nets == ("USB_D+", "USB_D-")
    assert "30.0 mm" in finding.evidence and "5.0 mm" in finding.evidence and " ps " in finding.evidence
    assert "estimate" in finding.evidence and "generic FR-4" in finding.evidence   # no layer models
    assert "USB" in finding.evidence and "full-speed USB" in finding.evidence
    assert "USB_D+" in finding.fix


def hs_part(ref, value, footprint, nets):
    return Component(ref, value, footprint, tuple(Pin(number, net) for number, net in nets.items()))


def hs_adapter_board(*parts, length=30):
    """A USB pair with a 30 mm / 5 mm skew and different via counts, on the given parts."""
    return replace(board(track("USB_D+", length), track("USB_D-", 5, y=1), via("USB_D+"), via("USB_D+", 1)),
                   components=parts)


def test_pair_that_reaches_only_a_charger_is_not_a_data_pair():
    connector = hs_part("J1", "USB4105", "Connector_USB:USB_C", {"A6": "USB_D+", "A7": "USB_D-"})
    charger = hs_part("U1", "BQ25792RQMR1", "Texas_RQM0029A_VQFN-HR-28", {"6": "USB_D+", "7": "USB_D-"})
    mcu = hs_part("U2", "STM32G431", "Package_QFP:LQFP-48", {"32": "USB_D-", "33": "USB_D+"})
    assert ids(hs_adapter_board(connector)) == ["hs.pair.skew", "hs.pair.vias"]
    assert ids(hs_adapter_board(connector, charger)) == []
    assert ids(hs_adapter_board(connector, charger, mcu)) == ["hs.pair.skew", "hs.pair.vias"]
    assert ids(hs_adapter_board(connector, mcu)) == ["hs.pair.skew", "hs.pair.vias"]


def test_matched_pair_and_small_mismatch_are_quiet():
    assert ids(board(track("USB_D+", 20), track("USB_D-", 19, y=1))) == []


def test_unrouted_or_half_routed_pair_is_not_a_mismatch():
    assert ids(board(track("USB_D+", 30))) == []
    assert ids(board(track("USB_D+", 30), via("USB_D-"))) == []   # a via alone is not routed track


def test_non_high_speed_pair_is_ignored():
    assert ids(board(track("MOTOR_P", 40), track("MOTOR_N", 2, y=1), via("MOTOR_P"), via("MOTOR_P", 1))) == []


def test_netlist_without_copper_gives_nothing():
    assert ids(DesignSnapshot((), "netlist")) == []


def test_layer_change_on_one_line_only_is_reported():
    snapshot = board(track("USB_D+", 10), track("USB_D+", 2, "B.Cu"), via("USB_D+"), via("USB_D+", 1),
                     track("USB_D-", 12, y=1))
    (finding,) = check_pair_vias(snapshot)
    assert finding.rule == "hs.pair.vias" and finding.nets == ("USB_D+", "USB_D-")
    assert "USB_D+: 2 vias, track on B.Cu, F.Cu" in finding.evidence
    assert "USB_D-: 0 vias, track on F.Cu" in finding.evidence


def test_single_extra_via_on_same_layers_is_normal():
    # USB-C: one line usually drops through one via to tie the second pad row.
    snapshot = board(track("USB_D+", 10), track("USB_D+", 1, "B.Cu"), via("USB_D+"),
                     track("USB_D-", 10, y=1), track("USB_D-", 1, "B.Cu", y=1), via("USB_D-"), via("USB_D-", 1))
    assert ids(snapshot) == []


def test_saved_board_layer_mismatch_and_info_severity():
    snapshot = DesignSnapshot((), "fixture", copper=read_copper_file(FIXTURE))
    found = [item for item in run_heavy(snapshot, providers=()) if item.rule.startswith("hs.")]
    assert [item.rule for item in found] == ["hs.pair.vias"]   # D+ on F.Cu and B.Cu, D- on B.Cu only
    assert all(item.severity == Severity.INFO for item in found)   # beta until measured
    skewed = board(track("USB_D+", 30), track("USB_D-", 5, y=1))
    assert [(item.rule, item.severity) for item in run_heavy(skewed, providers=()) if item.rule.startswith("hs.")] \
        == [("hs.pair.skew", Severity.INFO)]


@pytest.mark.skipif(not (BENCH / "seeded" / "KEY_hs_pairs.json").is_file(), reason="VelaTrace-bench not present")
def test_seeded_bench_boards():
    for case in json.loads((BENCH / "seeded" / "KEY_hs_pairs.json").read_text(encoding="utf-8")):
        snapshot = replace(DesignSnapshot((), "bench"), copper=read_copper_file(BENCH / case["board"]))
        assert ids(snapshot) == sorted(case["expected_rules"]), case["id"]
