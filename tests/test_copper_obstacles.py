import unittest

from velatrace.copper_obstacles import CopperObstacle, compile_copper_keepouts
from velatrace.errors import ValidationError
from velatrace.sexpr import children, one, parse


DSN = '''(pcb copper (unit mm) (structure
 (layer F.Cu (type signal)) (layer B.Cu (type signal))
 (boundary (rect pcb 0 -20 20 0))
 (rule (width 0.25) (clearance 0.2))
 (keepout "existing" (polygon F.Cu 0 1 2 3 4 5 6)))
 (network (net SIG (pins J1-1 J2-1))) (wiring))'''


class CopperObstacleTests(unittest.TestCase):
    def test_declared_grid_always_encloses_bounds_including_negative_positions(self):
        source = DSN.replace('(unit mm)', '(unit mm) (resolution um 10)')
        box = CopperObstacle('F.Cu', -1.00001, 2.00001, -.99999, 2.00009)
        result = parse(compile_copper_keepouts(source, (box,)))
        row = children(one(result, 'structure'), 'keepout')[-1]
        points = list(map(float, one(row, 'polygon')[3:]))
        self.assertEqual(points, [-1.0001, -2, -.9999, -2, -.9999, -2.0001, -1.0001, -2.0001])

    def test_compiles_box_with_one_y_flip_and_unit_scaling(self):
        source = DSN.replace("(unit mm)", "(unit um)")
        obstacle = CopperObstacle("F.Cu", 1.25, 2.5, 3.75, 5.0)

        compiled = parse(compile_copper_keepouts(source, (obstacle,)))
        structure = one(compiled, "structure")
        generated = [row for row in children(structure, "keepout") if str(row[1]).startswith("VelaTrace-")]
        self.assertEqual(len(generated), 1)
        polygon = one(generated[0], "polygon")
        self.assertEqual(polygon[1:3], ["F.Cu", "0"])
        self.assertEqual([float(value) for value in polygon[3:]],
                         [1250, -2500, 3750, -2500, 3750, -5000, 1250, -5000])

    def test_preserves_existing_rules_pins_and_keepouts(self):
        original = parse(DSN)
        result = parse(compile_copper_keepouts(DSN, (CopperObstacle("B.Cu", 2, 3, 4, 8),)))
        original_structure, result_structure = one(original, "structure"), one(result, "structure")
        self.assertEqual(one(original_structure, "rule"), one(result_structure, "rule"))
        self.assertEqual(children(original, "network"), children(result, "network"))
        self.assertEqual(children(original_structure, "keepout"), children(result_structure, "keepout")[:1])
        self.assertEqual(len(children(result_structure, "keepout")), 2)

    def test_names_are_stable_unique_and_skip_existing_names(self):
        source = DSN.replace('"existing"', '"VelaTrace-copper-obstacle-1"')
        obstacles = (CopperObstacle("B.Cu", 1, 1, 2, 2), CopperObstacle("F.Cu", 1, 1, 2, 2))
        first = compile_copper_keepouts(source, obstacles)
        second = compile_copper_keepouts(source, tuple(reversed(obstacles)))
        self.assertEqual(first, second)
        rows = children(one(parse(first), "structure"), "keepout")
        self.assertEqual([str(row[1]) for row in rows],
                         ["VelaTrace-copper-obstacle-1", "VelaTrace-copper-obstacle-2",
                          "VelaTrace-copper-obstacle-3"])

    def test_rejects_unknown_layers_and_malformed_obstacles(self):
        with self.assertRaises(ValidationError):
            compile_copper_keepouts(DSN, (CopperObstacle("In1.Cu", 1, 1, 2, 2),))
        for bounds in ((1, 1, 1, 2), (1, 2, 3, 2), (float("nan"), 1, 2, 3),
                       (0, 0, float("inf"), 3)):
            with self.subTest(bounds=bounds), self.assertRaises(ValidationError):
                CopperObstacle("F.Cu", *bounds)
        with self.assertRaises(ValidationError):
            CopperObstacle("", 0, 0, 1, 1)

    def test_requires_canonical_copper_layer_identifiers(self):
        for layer in ("F.cu", "User.Draw", "In0.Cu", "In31.Cu", "In01.Cu",
                      'F.Cu") (keepout injected'):
            with self.subTest(layer=layer), self.assertRaises(ValidationError):
                CopperObstacle(layer, 0, 0, 1, 1)
        self.assertEqual(CopperObstacle("In30.Cu", 0, 0, 1, 1).layer, "In30.Cu")

    def test_rejects_oversized_coordinates_and_non_copper_dsn_layer_types(self):
        with self.assertRaises(ValidationError):
            CopperObstacle("F.Cu", -1_000_001, 0, 0, 1)
        source = DSN.replace("(type signal)", "(type user)", 1)
        with self.assertRaises(ValidationError):
            compile_copper_keepouts(source, (CopperObstacle("F.Cu", 0, 0, 1, 1),))

    def test_empty_obstacles_validate_then_return_original_dsn_unchanged(self):
        self.assertIs(compile_copper_keepouts(DSN, ()), DSN)
        with self.assertRaises(ValidationError):
            compile_copper_keepouts("(pcb copper)", ())


if __name__ == "__main__":
    unittest.main()
