"""Hostile or damaged inputs end as a short ValidationError, never a Python traceback
or a long stall (VT-06, VT-10). All inputs are synthetic."""
import json
from pathlib import Path
import tempfile
import time
import unittest

from test_write_safety import BOARD, FakeBoard
from velatrace.candidate import checked_project, project_context
from velatrace.errors import ValidationError
from velatrace.preflight import _rule_notes
from velatrace.provider import ProviderConfig
from velatrace.ses import ViaSpec, number, parse_ses
from velatrace.sexpr import QuotedAtom, parse
from velatrace.write_safety import BoardSafety

FIXTURES = Path(__file__).parent / "fixtures" / "routing"


class TokenizerTests(unittest.TestCase):
    def test_one_huge_quoted_token_is_read_quickly(self):
        started = time.monotonic()
        root = parse('(a "' + "x" * 8_000_000 + '")', kicad=True)
        self.assertEqual(len(root[1]), 8_000_000)
        self.assertLess(time.monotonic() - started, 5)  # Was about 10 s for this size.

    def test_quoted_strings_keep_their_exact_meaning(self):
        self.assertEqual(parse(r'(a "x\"y\\z\n" "" "\q")', kicad=True), ["a", 'x"y\\z\n', "", "q"])
        self.assertIsInstance(parse('(a "b")', kicad=True)[1], QuotedAtom)
        # Specctra mode: a backslash is literal and ends nothing.
        self.assertEqual(parse('(a "C:\\dir\\")'), ["a", "C:\\dir\\"])
        joined = parse('(a "Pi-1"-3)')[1]
        self.assertEqual((str(joined), joined.raw), ("Pi-1-3", '"Pi-1"-3'))
        for text in ('(a "open', '(a "open\\', '(a "open\\"'):
            with self.subTest(text=text), self.assertRaisesRegex(ValidationError, "Unterminated"):
                parse(text, kicad=True)

    def test_token_limit_is_reported_as_a_size_limit(self):
        with self.assertRaisesRegex(ValidationError, "2,000,000 tokens.*not supported"):
            parse("(" + "a " * 2_000_001 + ")")


class ProjectFileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.board = Path(temp.name) / "b.kicad_pcb"
        self.board.write_text(BOARD, encoding="utf-8")

    def test_unexpected_project_shapes_are_refused_cleanly(self):
        settings = lambda **values: {"board": {"design_settings": values}}
        cases = {"rule_severities is a list": settings(rule_severities=["ignore"]),
                 "via_dimensions is a dict": settings(via_dimensions={"a": 1}),
                 "via row is a string": settings(via_dimensions=["x"]),
                 "via size is text": settings(via_dimensions=[{"diameter": "nan", "drill": "inf"}]),
                 "rules is a list": settings(rules=[]),
                 "net_settings is a list": {**settings(), "net_settings": []},
                 "class row is a number": {**settings(), "net_settings": {"classes": [5]}},
                 "class via size is text": {**settings(), "net_settings": {"classes": [{"via_diameter": "1", "via_drill": 1}]}},
                 "root is a list": [], "board is a list": {"board": []}}
        project = self.board.with_suffix(".kicad_pro")
        for name, value in cases.items():
            with self.subTest(name):
                project.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(ValidationError, "missing or malformed"):
                    project_context(self.board)
                try:
                    checked_project(project.read_bytes())
                except ValidationError:
                    pass
                _rule_notes(value, self.board)  # Notes never raise.
        project.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
        for read in (lambda: project_context(self.board), lambda: checked_project(project.read_bytes())):
            with self.assertRaisesRegex(ValidationError, "missing or malformed"):
                read()

    def test_a_file_named_velatrace_is_explained(self):
        (self.board.parent / ".velatrace").write_text("not a folder", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, r"\.velatrace beside the board.*taken by a file"):
            BoardSafety(FakeBoard(self.board), self.board)


class RouterOutputTests(unittest.TestCase):
    expected = dict(expected_design="simple-test.dsn", nets={"N"}, layers={"F.Cu", "B.Cu"},
                    via_catalog={"Via[0-1]_600:300_um": ViaSpec(.6, .3, ("F.Cu", "B.Cu"))},
                    expected_placements={"J1": (5, 10, "front", 0), "J2": (25, 10, "front", 0)},
                    expected_placement_resolution_mm=.0001)

    def test_lists_where_names_belong_are_refused(self):
        ses = (FIXTURES / "freerouting-2.1.0.ses").read_text(encoding="utf-8")
        parse_ses(ses, **self.expected)
        for old, new in (("(resolution um 10)", "(resolution (um) 10)"), ("(routes", "((routes)"),
                         ("(network_out", "((x) "), ("(net N", "(net (N)"), ("(path F.Cu", "(path (F.Cu)")):
            self.assertIn(old, ses)
            with self.subTest(new=new), self.assertRaises(ValidationError):
                parse_ses(ses.replace(old, new, 1), **self.expected)

    def test_only_plain_ascii_decimals_are_numbers(self):
        for good, value in (("10", 10), ("-0.5", -.5), ("+3.", 3), (".25", .25), ("1e-05", 1e-5), ("2.5E3", 2500)):
            self.assertEqual(number(good), value)
        for bad in ("1_0", "\u0663", "\uff11", "1\u00a0", " 1", "0x10", "nan", "inf", "-inf", "1e", "", ".", "1,5", ["1"], None):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                number(bad)


class ProviderConfigTests(unittest.TestCase):
    def config(self, endpoint="https://example.invalid/v1", model="fixture", **values):
        return ProviderConfig("Fixture", endpoint, model, "test-key", **values)

    def test_out_of_range_port_is_a_validation_error(self):
        with self.assertRaisesRegex(ValidationError, "invalid port"):
            self.config("https://example.invalid:99999/v1")
        self.config("https://example.invalid:8443/v1")

    def test_model_id_cannot_walk_the_request_path(self):
        for model in ("../../v1/files", "a/../b", "/abs", "a//b", "a/", ".."):
            for protocol in ("gemini", "openai"):
                with self.subTest(model=model, protocol=protocol), self.assertRaisesRegex(ValidationError, "model"):
                    self.config(model=model, protocol=protocol)
        with self.assertRaisesRegex(ValidationError, "model"):
            self.config(model="models/gemini-x", protocol="gemini")  # Becomes part of the URL path.
        self.config(model="gemini-2.5-flash", protocol="gemini")
        self.config(model="meta-llama/llama-4-scout", protocol="openai", search_model="groq/compound")


if __name__ == "__main__":
    unittest.main()
