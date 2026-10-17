"""Copper snapshot readers, the high-speed net finder and the per-net delay helper."""
import math
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest

from velatrace.copper import (FilledZone, NetClassInfo, find_high_speed, interface_guess, net_routing,
                              parse_copper, read_copper_file, read_copper_ipc)
from velatrace.ipc import KiCadReader
from velatrace.stackup import homogeneous_delay_ps
from velatrace.vela_electrical import FR4_EPSILON_R

BOARD = Path(__file__).parent / "fixtures" / "copper" / "hs_board.kicad_pcb"


def fake_board(text, classes=None):
    from kipy.board_types import Net
    classes = classes or {}
    return SimpleNamespace(
        get_as_string=lambda: text,
        get_nets=lambda: [Net(name=name) for name in classes],
        get_netclass_for_nets=lambda nets: {net.name: classes[net.name] for net in nets})


class FileReaderTests(unittest.TestCase):
    def setUp(self):
        self.copper = read_copper_file(BOARD)

    def test_layers_tracks_and_arc_length(self):
        c = self.copper
        self.assertEqual(c.layers, ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
        self.assertEqual(c.thickness_mm, 1.6)
        self.assertEqual(len(c.segments), 4)
        arc = next(s for s in c.segments if s.layer == "B.Cu" and s.net == "/USB_D+")
        self.assertAlmostEqual(arc.length_mm, math.pi / 2, delta=0.01)
        self.assertEqual((arc.start, arc.end), ((11.0, 5.0), (10.0, 6.0)))
        # KiCad 10 inline net names read the same as KiCad 9 net codes.
        self.assertEqual({s.net for s in c.segments}, {"/USB_D+", "/USB_D-"})

    def test_via_spans_zones_and_planes(self):
        c = self.copper
        through, blind = c.vias
        self.assertTrue(through.through(c.layers))
        self.assertEqual(blind.layers, ("F.Cu", "In1.Cu"))
        self.assertFalse(blind.through(c.layers))
        self.assertEqual(c.zones, (FilledZone("GND", "In1.Cu", ((0, 0), (20, 0), (20, 10), (0, 10))),))
        # In1 by its ground fill, In2 by its KiCad "power" layer type.
        self.assertEqual(c.planes, {"In1.Cu", "In2.Cu"})
        self.assertEqual([c.models[name].kind for name in c.layers], ["microstrip", "plane", "plane", "microstrip"])
        self.assertTrue(c.models["F.Cu"].assumed)   # no stackup section: FR-4 default

    def test_net_classes_from_project(self):
        classes = self.copper.net_classes
        self.assertEqual(classes["/USB_D+"], NetClassInfo("USB", 0.15, 0.2))
        self.assertEqual(classes["GND"].name, "Default")

    def test_no_project_means_no_net_classes(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        board = Path(folder) / "board.kicad_pcb"
        shutil.copy(BOARD, board)
        self.assertEqual(read_copper_file(board).net_classes, {})

    def test_two_layer_board_uses_the_filled_side_as_reference(self):
        text = BOARD.read_text(encoding="utf-8").replace('(4 "In1.Cu" signal)', "").replace(
            '(6 "In2.Cu" power)', "").replace('"In1.Cu"', '"B.Cu"')
        c = parse_copper(text)
        self.assertEqual((c.layers, c.planes), (("F.Cu", "B.Cu"), {"B.Cu"}))
        self.assertEqual(c.models["F.Cu"].kind, "microstrip")


class IpcReaderTests(unittest.TestCase):
    def test_live_board_gives_same_copper_and_kicad_net_classes(self):
        usb = SimpleNamespace(name="USB", diff_pair_track_width=150_000, diff_pair_gap=200_000)
        live = read_copper_ipc(fake_board(BOARD.read_text(encoding="utf-8"), {"/USB_D+": usb}))
        saved = read_copper_file(BOARD)
        self.assertEqual((live.segments, live.vias, live.zones, live.planes),
                         (saved.segments, saved.vias, saved.zones, saved.planes))
        self.assertEqual(live.net_classes, {"/USB_D+": NetClassInfo("USB", 0.15, 0.2)})

    def test_read_board_attaches_copper_and_survives_failure(self):
        from kipy.proto.board.board_types_pb2 import BoardLayer
        board = fake_board(BOARD.read_text(encoding="utf-8"))
        board.get_footprints = lambda: []
        board.get_enabled_layers = lambda: [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu]
        board.name = ""
        client = SimpleNamespace(get_version=lambda: SimpleNamespace(major=10, minor=0, patch=6),
                                 get_board=lambda: board)
        snapshot = KiCadReader(client).read_board(copper=True)
        self.assertEqual(len(snapshot.copper.segments), 4)
        self.assertIsNone(KiCadReader(client).read_board().copper)

        def broken():
            raise RuntimeError("no IPC")
        board.get_as_string = broken
        snapshot = KiCadReader(client).read_board(copper=True)
        self.assertIsNone(snapshot.copper)
        self.assertTrue(any("high-speed" in warning for warning in snapshot.warnings))


class FinderTests(unittest.TestCase):
    def test_true_positives(self):
        for net, interface in [("USB_D+", "usb"), ("/USB/USB_DP", "usb"), ("D-", "usb"), ("USB1_D_N", "usb"), ("USB_SSTX_P", "usb"),
                               ("ETH_TX_P", "ethernet"), ("TD+", "ethernet"), ("RGMII_TXD0", "ethernet"),
                               ("HDMI_D0+", "hdmi"), ("TMDS_CLK_N", "hdmi"), ("MIPI_CSI_D0_P", "lvds/mipi"),
                               ("LVDS_CLK+", "lvds/mipi"), ("DSI_D1_N", "lvds/mipi"), ("EPHY_RX_P", "ethernet"), ("SD_CLK", "sdio/sd"), ("SDIO_D2", "sdio/sd"),
                               ("QSPI_SCLK", "qspi"), ("/QSPI_SD0", "qspi"), ("RF_OUT", "rf"), ("ANT1", "rf"),
                               ("XTAL_IN", "clock"), ("XIN", "clock"), ("REFCLK_P", "clock")]:
            self.assertEqual(interface_guess(net), interface, net)

    def test_true_negatives(self):
        for net in ["MOTOR_P", "MOTOR_N", "LED_P", "SW_P", "USB_VBUS", "VBUS", "USB_CC1", "HDMI_CEC",
                    "HDMI_HPD", "ETH_LED", "SPI_CLK", "I2S_SD", "GND", "Net-(U1-D+)", "RFID_EN", "DP", "MIPI_PWDN",
                    "MIPI_SDA", "CAN_P", "USB_PRESS"]:
            self.assertIsNone(interface_guess(net), net)

    def test_pairs_need_a_fast_hint(self):
        names = ["USB_D+", "USB_D-", "D+", "D-", "ETH_TX_P", "ETH_TX_N", "MOTOR_P", "MOTOR_N",
                 "SENSE_P", "SENSE_N", "LED_P", "SW_P"]
        found = find_high_speed(names, {"SENSE_P": NetClassInfo("Diff_90R"), "SENSE_N": NetClassInfo("Diff_90R")})
        self.assertEqual(set(found.pairs), {("USB_D+", "USB_D-"), ("D+", "D-"), ("ETH_TX_P", "ETH_TX_N"),
                                            ("SENSE_P", "SENSE_N")})
        self.assertNotIn("SENSE_P", found.interfaces)   # a fast class, but no interface guess
        self.assertEqual(find_high_speed(["A_P", "A_N"], {"A_P": NetClassInfo("USB")}).interfaces, {"A_P": "usb"})


class DelayTests(unittest.TestCase):
    def test_length_delay_vias_and_layers(self):
        c = read_copper_file(BOARD)
        routing = net_routing(c)
        dp = routing["/USB_D+"]
        top, bottom = c.models["F.Cu"], c.models["B.Cu"]
        arc = next(s.length_mm for s in c.segments if s.layer == "B.Cu" and s.net == "/USB_D+")
        self.assertAlmostEqual(dp.length_mm, 7 + arc)
        expected = (7 * top.delay_ps_per_mm(0.2) + arc * bottom.delay_ps_per_mm(0.2)
                    + homogeneous_delay_ps(1.6, FR4_EPSILON_R))
        self.assertAlmostEqual(dp.delay_ps, expected)
        self.assertEqual((dp.vias, dp.layers, dp.assumed), (1, {"F.Cu", "B.Cu"}, True))
        # A blind via F.Cu to In1.Cu is one third of the stack.
        dn = routing["/USB_D-"]
        self.assertAlmostEqual(dn.delay_ps, 4 * bottom.delay_ps_per_mm(0.2)
                               + homogeneous_delay_ps(1.6 / 3, FR4_EPSILON_R))
        self.assertNotIn("GND", routing)   # zone fill only: no tracks or vias
