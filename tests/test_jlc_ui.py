"""JLCPCB window, offscreen: table, pending assignments, explicit confirmations. No network."""
import csv
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from PySide6.QtWidgets import QApplication
    from velatrace import jlc_ui
except ImportError:
    QApplication = None

from jlc_fixture import install, live_payload
from velatrace import jlc_live
from velatrace.models import Component, DesignSnapshot, Pin


def comp(ref, value, footprint, lcsc=""):
    return Component(ref, value, footprint, (Pin("1", "N" + ref), Pin("2", "GND")),
                     {"LCSC": lcsc} if lcsc else {})


@unittest.skipIf(QApplication is None, "Install the pinned Qt UI dependency")
class JlcDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name)
        install(self.config).close()
        snapshot = DesignSnapshot((
            comp("C1", "100n", "Capacitor_SMD:C_0402_1005Metric", "C1608"),
            comp("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
            comp("R2", "10k", "Resistor_SMD:R_0402_1005Metric"),
            comp("U1", "RP2040", "Package_DFN_QFN:QFN-56", "C2040")), "test")
        self.window = SimpleNamespace(config_dir=self.config, audit_snapshot=snapshot, safety=None,
                                      settings=SimpleNamespace(cli="kicad-cli"))
        self.dialog = jlc_ui.JlcDialog(self.window)
        self.addCleanup(self.dialog.reject)
        # Jobs run inline here; the thread hand-off is exercised in the real application.
        self.dialog.run = lambda title, operation, done: (done(operation(lambda *a: None)), self.dialog.reload())

    def cells(self, column):
        index = jlc_ui.COLUMNS.index(column)
        return [self.dialog.table.item(row, index).text() for row in range(self.dialog.table.rowCount())]

    def test_table_shows_every_line_offline(self):
        self.assertEqual(self.cells("Parts"), ["C1", "R1, R2", "U1"])
        self.assertEqual(self.cells("LCSC"), ["C1608", "suggest C25744", "C2040"])
        self.assertEqual(self.cells("Tier"), ["Extended", "—", "Extended"])
        self.assertEqual(self.cells("Stock"), ["60000", "", "72000"])
        self.assertEqual(self.cells("Ext. fee"), ["$3.00", "", "$3.00"])
        self.assertIn("published 2026-09-26", self.dialog.source.text())
        self.assertIn("at 5", self.dialog.cost.text())
        self.assertFalse(self.dialog.write_button.isEnabled())   # nothing pending, no open PCB
        self.assertFalse(self.dialog.export_button.isEnabled())
        self.assertIn("open PCB", self.dialog.export_button.toolTip())

    def test_suggestions_are_pending_until_written_and_csv_lists_them(self):
        self.dialog.use_suggestions()
        self.assertEqual(self.cells("LCSC")[1], "C25744 (pending)")
        self.assertEqual(self.cells("Tier")[1], "Basic")
        self.assertEqual(self.dialog.pending, {"R1": "C25744", "R2": "C25744"})
        self.assertFalse(self.dialog.changed)                    # the design itself is untouched
        self.assertEqual(self.window.audit_snapshot.components[1].fields, {})
        self.assertIn("open PCB", self.dialog.write_button.toolTip())
        target = self.config / "assign.csv"
        with patch.object(jlc_ui.QFileDialog, "getSaveFileName", return_value=(str(target), "")):
            self.dialog.export_csv()
        rows = list(csv.reader(target.open(encoding="utf-8")))
        self.assertEqual(rows[1:], [["C1", "100n", "Capacitor_SMD:C_0402_1005Metric", "C1608"],
                                    ["R1", "10k", "Resistor_SMD:R_0402_1005Metric", "C25744"],
                                    ["R2", "10k", "Resistor_SMD:R_0402_1005Metric", "C25744"],
                                    ["U1", "RP2040", "Package_DFN_QFN:QFN-56", "C2040"]])

    def test_search_dialog_prefills_value_and_returns_a_choice(self):
        search = jlc_ui.SearchDialog(self.dialog, self.dialog.catalogue, self.dialog.lines[0])
        self.assertEqual([p.lcsc for p in search.results], ["C1525", "C307331", "C1608"])
        search.economic.setChecked(True)
        self.assertEqual([p.lcsc for p in search.results], ["C1525", "C307331"])
        search.query.setText("stm32")
        search.same_value.setChecked(False)
        search.package.setText("")
        self.assertEqual([p.lcsc for p in search.results], ["C8734"])
        search.choose()
        self.assertEqual(search.code, "C8734")

    def test_write_needs_confirmation_and_one_call(self):
        calls = []
        self.window.safety = SimpleNamespace(path=self.config / "b.kicad_pcb")
        self.dialog.set_pending(("R1", "R2"), "C25744")
        result = SimpleNamespace(summary="done")
        with patch.object(jlc_ui.jlc_assign, "assign_lcsc", side_effect=lambda s, a: calls.append(a) or result):
            with patch.object(jlc_ui, "_confirm", return_value=False):
                self.dialog.write_board()
            self.assertEqual(calls, [])
            with patch.object(jlc_ui, "_confirm", return_value=True) as confirm:
                self.dialog.write_board()
        self.assertIn("one Undo step", confirm.call_args.args[2])
        self.assertIn("Update Schematic from PCB", confirm.call_args.args[2])
        self.assertEqual(calls, [{"R1": "C25744", "R2": "C25744"}])
        self.assertEqual((self.dialog.pending, self.dialog.changed), ({}, True))
        self.assertEqual(self.dialog.base.components[1].fields, {"LCSC": "C25744"})

    def test_live_refresh_asks_first_and_labels_results(self):
        sent, real = [], jlc_live.refresh

        def refresh(config, codes, **kwargs):
            sent.append(list(codes))
            return real(config, codes, sleep=lambda s: None,
                                    fetch=lambda url, **k: (200, {}, live_payload(url.rsplit("=", 1)[-1], stock=5)))
        with patch.object(jlc_ui.jlc_live, "refresh", side_effect=refresh):
            with patch.object(jlc_ui, "_confirm", return_value=False):
                self.dialog.refresh_live()
            self.assertEqual(sent, [])
            with patch.object(jlc_ui, "_confirm", return_value=True) as confirm:
                self.dialog.refresh_live()
        self.assertIn("2 LCSC part number(s)", confirm.call_args.args[2])
        self.assertIn("cart.jlcpcb.com", confirm.call_args.args[2])
        self.assertEqual(sent, [["C1608", "C2040"]])
        self.assertEqual(self.cells("Stock"), ["5", "", "5"])
        self.assertTrue(self.cells("Data")[0].startswith("live as of "))

    def test_catalogue_download_states_size_and_can_be_declined(self):
        info = jlc_ui.jlc_catalog.RemoteInfo((("a", 180_567_213),), "2026-09-26T10:41:07Z")
        with patch.object(jlc_ui.jlc_catalog, "remote_info", return_value=info), \
                patch.object(jlc_ui.jlc_catalog, "download") as download, \
                patch.object(jlc_ui, "_confirm", return_value=False) as confirm:
            self.dialog.download_catalogue()
        self.assertIn("181 MB", confirm.call_args.args[2])
        self.assertIn("No board data", confirm.call_args.args[2])
        download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
