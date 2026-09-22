from pathlib import Path
import hashlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from velatrace.constraints import Constraint, Scope
from velatrace.dsn import DsnInput, ExportTicket, dsn_scale, file_digest
from velatrace.errors import CapabilityError, ValidationError
from velatrace.freerouting import (Freerouting, PROBE_SHA256, clean_environment,
                                  constrained_dsn, run_bounded)
from velatrace.ses import ViaSpec, parse_ses
from velatrace.sexpr import children, one, parse

FIXTURES = Path(__file__).parent / "fixtures" / "routing"


class FreeroutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_missing_router_fails_startup_with_install_help(self):
        with self.assertRaisesRegex(CapabilityError, "Freerouting is missing.*2.1.0"):
            Freerouting(self.root / "missing.jar", work_directory=self.root).check_startup()

    def test_missing_java_and_modified_jar_refused(self):
        jar = self.root / "test.jar"
        jar.write_bytes(b"unit test")
        router = Freerouting(jar, self.root / "missing-java", work_directory=self.root)
        with self.assertRaisesRegex(CapabilityError, "hash mismatch"):
            router.check_startup()
        with patch("velatrace.freerouting.JAR_SHA256", hashlib.sha256(jar.read_bytes()).hexdigest()):
            with self.assertRaisesRegex(CapabilityError, "Java is missing"):
                router.check_startup()

    def test_environment_never_inherits_keys_java_options_or_router_settings(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "secret", "JAVA_TOOL_OPTIONS": "evil",
                                     "CLASSPATH": "evil", "FREEROUTING__API_SERVER__ENABLED": "true"}):
            env = clean_environment(self.root)
        self.assertTrue(set(env) <= {"SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "TMP", "TEMP", "APPDATA", "LOCALAPPDATA", "SystemRoot", "windir"})
        self.assertNotIn("secret", env.values())

    def test_bounded_process_output_and_timeout(self):
        result = run_bounded([sys.executable, "-c", "print('a'*100000)"], self.root, 10)
        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.output), 65536)
        with self.assertRaisesRegex(CapabilityError, "exceeded"):
            run_bounded([sys.executable, "-c", "import time; time.sleep(10)"], self.root, .1)

    def test_dsn_and_ses_use_different_coordinate_scaling(self):
        root = parse((FIXTURES / "simple.dsn").read_text())
        self.assertEqual(dsn_scale(root), .001)
        self.assertEqual(dsn_scale(parse('(pcb x (resolution um 10))')), .001)

    def test_constraints_raise_every_rule_and_preserve_quoted_atoms(self):
        original = (FIXTURES / "simple.dsn").read_text().replace("(net N", '(net "N"')
        constraints = (Constraint("w", Scope.SESSION, "trace-width", "all nets", .5),
                       Constraint("c", Scope.SESSION, "clearance", "all nets", .3))
        output = constrained_dsn(original, constraints)
        self.assertIn('(net "N"', output)
        root = parse(output)
        rule = one(one(root, "structure"), "rule")
        self.assertEqual(one(rule, "width"), ["width", "500"])
        self.assertEqual(one(rule, "clearance"), ["clearance", "300"])
        self.assertEqual(constrained_dsn(output, (Constraint("w", Scope.SESSION, "trace-width", "all nets", .2),)), output)
        with self.assertRaises(CapabilityError):
            constrained_dsn(original, (Constraint("h", Scope.SESSION, "header-clearance", "all header footprints", 2),))
        with self.assertRaises(ValidationError):
            constrained_dsn('(pcb x (unit um) (structure))', constraints)

    def parse_actual(self, text=None):
        return parse_ses(text or (FIXTURES / "freerouting-2.1.0.ses").read_text(),
                         expected_design="simple-test.dsn", nets={"N"}, layers={"F.Cu", "B.Cu"},
                         via_catalog={"Via[0-1]_600:300_um": ViaSpec(.6, .3, ("F.Cu", "B.Cu"))},
                         expected_placements={"J1": (5, 10, "front", 0), "J2": (25, 10, "front", 0)})

    def test_published_router_actual_ses_all_metadata_and_width(self):
        plan = self.parse_actual()
        self.assertEqual(plan.trace_count, 1)
        self.assertEqual(plan.tracks[0].width_mm, .5)
        self.assertEqual(set(plan.tracks[0].points_mm), {(5., 10.), (25., 10.)})

    def test_changed_placement_rotation_side_or_geometry_refuses_whole_ses(self):
        text = (FIXTURES / "freerouting-2.1.0.ses").read_text()
        for altered in (text.replace("front 0", "front 90", 1), text.replace("front 0", "back 0", 1),
                        text.replace("place J1 50000", "place J1 50001", 1),
                        text.replace("(circle F.Cu 6000", "(circle F.Cu 7000", 1),
                        text.replace("(path F.Cu", "(arc F.Cu", 1)):
            with self.assertRaises(ValidationError):
                self.parse_actual(altered)

    def test_packaged_policy_probe_hash(self):
        import velatrace.freerouting as module
        probe = Path(module.__file__).parent / "router_resources" / "OfflineProbe.class"
        self.assertEqual(hashlib.sha256(probe.read_bytes()).hexdigest(), PROBE_SHA256)


@unittest.skipUnless(os.environ.get("VELATRACE_TEST_JAR") and os.environ.get("VELATRACE_TEST_JAVA"),
                     "Set VELATRACE_TEST_JAR and VELATRACE_TEST_JAVA for the optional real external-process test")
class NativeFreeroutingTests(unittest.TestCase):
    def test_real_offline_router_and_numeric_width(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            board = directory / "simple-test.kicad_pcb"
            board.write_text("Adapter-only fixture: not a candidate PCB or DRC claim.")
            path = directory / "simple-test.dsn"
            path.write_text((FIXTURES / "simple.dsn").read_text())
            dsn = DsnInput(path, file_digest(path), ExportTicket.begin(board),
                           frozenset({"N"}), frozenset({"F.Cu", "B.Cu"}))
            router = Freerouting(Path(os.environ["VELATRACE_TEST_JAR"]), os.environ["VELATRACE_TEST_JAVA"],
                                 work_directory=directory / "processes", timeout_seconds=60)
            for constraints, width in (((), .25), ((Constraint("w", Scope.SESSION, "trace-width", "all nets", .5),
                                                    Constraint("c", Scope.SESSION, "clearance", "all nets", .3)), .5)):
                result = router.route(dsn, constraints)
                plan = FreeroutingTests.parse_actual(self, result)
                self.assertEqual(plan.tracks[0].width_mm, width)
                self.assertIn('access denied ("java.net.URLPermission"', router.last_log)
                self.assertFalse(any((directory / "processes").iterdir()))


if __name__ == "__main__":
    unittest.main()
