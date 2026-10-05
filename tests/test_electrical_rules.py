from dataclasses import FrozenInstanceError
from pathlib import Path
import unittest

from velatrace.electrical_rules import (ElectricalRules, NetRule, compile_electrical_dsn,
                                       validate_plan_rules)
from velatrace.errors import ValidationError
from velatrace.ses import RoutePlan, Track, Via, ViaSpec
from velatrace.sexpr import children, one, parse


LAYERS = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
DSN = '''(pcb "C:\\board files\\demo.dsn"
 (parser (string_quote ") (space_in_quoted_tokens on))
 (resolution um 10) (unit um)
 (structure
  (layer F.Cu (type signal)) (layer In1.Cu (type power))
  (layer In2.Cu (type signal)) (layer B.Cu (type signal))
  (via Through) (rule (width 250) (clearance 200)))
 (network
  (net "signal N" (pins "Pi-1"-3 U2-1)) (net Q (pins U1-2 U2-2))
  (class Default "signal N" Q
   (circuit (use_via Through) (length 20000 0))
   (rule (width 300) (clearance 220) (clearance 280 (type smd_smd)))
   (layer_rule B.Cu (rule (width 350) (clearance 240)))))
 (wiring))'''


def profile(*rules):
    return ElectricalRules(tuple(rules))


def generated(text, net="signal N"):
    network = one(parse(text), "network")
    return next(node for node in children(network, "class")
                if net in [x for x in node[2:] if isinstance(x, str)])


def layer_rule(node, layer):
    return one(next(row for row in children(node, "layer_rule") if row[1] == layer), "rule")


class ElectricalCompilerTests(unittest.TestCase):
    def test_split_preserves_other_net_and_quoted_joined_atoms(self):
        policy = profile(NetRule("signal N", ("F.Cu", "B.Cu"), (("F.Cu", .4),)))
        output = compile_electrical_dsn(DSN, policy)
        self.assertIn('"Pi-1"-3', output)
        self.assertIn('"C:\\board files\\demo.dsn"', output)
        self.assertIn('(string_quote ")', output)
        before, after = one(parse(DSN), "network"), one(parse(output), "network")
        self.assertEqual(children(before, "net"), children(after, "net"))
        old = children(after, "class")[0]
        expected_old = children(before, "class")[0]
        expected_old.remove("signal N")
        self.assertEqual(old, expected_old)
        new = generated(output)
        self.assertEqual(one(one(new, "circuit"), "use_via"), ["use_via", "Through"])
        self.assertEqual(one(one(new, "circuit"), "length"), ["length", "20000", "0"])
        self.assertEqual(one(one(new, "circuit"), "use_layer"), ["use_layer", "F.Cu", "B.Cu"])
        self.assertEqual(one(layer_rule(new, "F.Cu"), "width"), ["width", "400"])
        self.assertEqual(one(layer_rule(new, "B.Cu"), "width"), ["width", "350"])
        self.assertEqual(children(one(new, "rule"), "clearance")[1:],
                         children(one(old, "rule"), "clearance")[1:])
        self.assertEqual(one(new, "rule")[1], ["clearance", "280"])

    def test_multiple_nets_split_from_same_class_without_losing_membership(self):
        output = compile_electrical_dsn(DSN, profile(NetRule("signal N", LAYERS), NetRule("Q", LAYERS)))
        classes = children(one(parse(output), "network"), "class")
        self.assertEqual([x for x in classes[0][2:] if isinstance(x, str)], [])
        self.assertEqual([x for x in classes[1][2:] if isinstance(x, str)], ["signal N"])
        self.assertEqual([x for x in classes[2][2:] if isinstance(x, str)], ["Q"])

    def test_same_net_and_class_name_preserves_class_name(self):
        output = compile_electrical_dsn(DSN.replace('class Default', 'class Q'), profile(NetRule("Q", LAYERS)))
        self.assertEqual(children(one(parse(output), "network"), "class")[0][1], "Q")

    def test_generated_class_name_collision_is_avoided(self):
        output = compile_electrical_dsn(DSN.replace("class Default", "class VelaTrace_Electrical_0"),
                                        profile(NetRule("Q", LAYERS)))
        self.assertEqual(generated(output, "Q")[1], "VelaTrace_Electrical_0_")

    def test_units_are_dsn_units_not_ses_resolution(self):
        for unit, raw, expected in (("um", "250", "500"), ("mm", ".25", ".5"),
                                    ("mil", "10", "20"), ("inch", ".01", ".02")):
            text = f'''(pcb x (resolution {unit} 10) (unit {unit})
             (structure (layer F.Cu (type signal)) (rule (width {raw}) (clearance {raw})))
             (network (net N (pins A-1 B-1))) )'''
            scale = {"um": .001, "mm": 1, "mil": .0254, "inch": 25.4}[unit]
            with self.subTest(unit=unit):
                output = compile_electrical_dsn(text, profile(NetRule("N", ("F.Cu",),
                                                                     (("F.Cu", float(expected) * scale),))))
                actual = one(layer_rule(generated(output, "N"), "F.Cu"), "width")[1]
                self.assertAlmostEqual(float(actual), float(expected))

    def test_existing_use_layer_is_intersected_without_changing_use_via(self):
        text = DSN.replace("(use_via Through)", "(use_via Through) (use_layer F.Cu In2.Cu)")
        new = generated(compile_electrical_dsn(text, profile(NetRule("signal N", ("F.Cu", "B.Cu")))))
        self.assertEqual(one(one(new, "circuit"), "use_layer"), ["use_layer", "F.Cu"])
        for rule in (NetRule("signal N", ("B.Cu",)),
                     NetRule("signal N", ("F.Cu", "B.Cu"), (("B.Cu", .4),))):
            with self.assertRaisesRegex(ValidationError, "conflict"):
                compile_electrical_dsn(text, profile(rule))

    def test_existing_stricter_clearance_is_preserved_and_typed_rules_are_raised(self):
        for requested, untyped, typed in ((.23, 230, 280), (.4, 400, 400)):
            new = generated(compile_electrical_dsn(DSN, profile(NetRule("signal N", LAYERS,
                                                                                      clearance_mm=requested))))
            rows = children(layer_rule(new, "F.Cu"), "clearance")
            self.assertEqual(float(rows[0][1]), untyped)
            self.assertEqual(float(rows[1][1]), typed)
            self.assertEqual(rows[1][2], ["type", "smd_smd"])
            self.assertGreaterEqual(float(children(layer_rule(new, "B.Cu"), "clearance")[0][1]), 240)

    def test_cross_class_clearance_is_seeded_before_scoped_clearances(self):
        text = DSN.replace('(width 350) (clearance 240)', '(width 350) (clearance 500)')
        for requested, expected in ((None, "500"), (.7, "700")):
            new = generated(compile_electrical_dsn(text, profile(NetRule("signal N", LAYERS,
                                                                                        clearance_mm=requested))))
            # In Freerouting 2.1 this FIRST class-wide clearance seeds pairs with
            # every other class; per-layer rules only change internal pairs.
            self.assertEqual(one(new, "rule")[1], ["clearance", expected])

    def test_narrower_widths_are_refused_at_global_class_and_layer_levels(self):
        for layer, width in (("F.Cu", .29), ("B.Cu", .34)):
            with self.subTest(layer=layer), self.assertRaisesRegex(ValidationError, "weaken"):
                compile_electrical_dsn(DSN, profile(NetRule("signal N", LAYERS, ((layer, width),))))
        simple = (Path(__file__).parent / "fixtures/routing/simple.dsn").read_text()
        with self.assertRaisesRegex(ValidationError, "weaken"):
            compile_electrical_dsn(simple, profile(NetRule("N", ("F.Cu",), (("F.Cu", .2),))))

    def test_ambiguous_and_unsupported_dsn_rules_fail_closed(self):
        mutations = (
            ('(net Q', '(net "signal N"'),
            ('class Default', 'class Default'),  # duplicate class inserted below
            ('Default "signal N" Q', 'Default "signal N" Q Q'),
            ('(width 300)', '(width 300) (width 400)'),
            ('(clearance 220)', '(clearance 220) (clearance 230)'),
            ('(use_via Through)', '(use_via Through) (use_layer Ghost)'),
            ('layer_rule B.Cu', 'layer_rule Ghost'),
            ('(width 300)', '(width 300 400)'),
            ('(width 300)', '(impedance 50)'),
            ('smd_smd', 'smd_via_same_net'),
            ('smd_smd', 'smd_to_turn_gap'),
            ('(length 20000 0)', '(length garbage 0)'),
            ('(pins U1-2 U2-2)', '(pins U1-2 U2-2) (layer_rule F.Cu (rule (width 300)))'),
            ('(type power)', '(type power) (rule (impedance 50))'),
        )
        for old, new in mutations:
            text = DSN.replace(old, new)
            if old == new:
                text = text.replace('(net Q', '(class Default Q) (net Q')
            with self.subTest(new=new), self.assertRaises(ValidationError):
                compile_electrical_dsn(text, profile(NetRule("signal N", LAYERS)))

    def test_unknown_profile_net_layer_and_duplicate_layers_refused(self):
        for rule in (NetRule("missing", LAYERS), NetRule("Q", ("Ghost",))):
            with self.assertRaises(ValidationError):
                compile_electrical_dsn(DSN, profile(rule))
        with self.assertRaises(ValidationError):
            compile_electrical_dsn(DSN.replace("layer In2.Cu", "layer In1.Cu"),
                                   profile(NetRule("Q", LAYERS)))

    def test_structure_layer_rule_grammar_and_inherited_width_are_verified(self):
        text = '''(pcb x (unit mm) (structure
         (layer F.Cu (type signal) (rule (width .6)))
         (rule (width .25) (clearance .2))) (network (net N (pins A-1 B-1))))'''
        with self.assertRaisesRegex(ValidationError, "weaken"):
            compile_electrical_dsn(text, profile(NetRule("N", ("F.Cu",), (("F.Cu", .5),))))
        output = compile_electrical_dsn(text, profile(NetRule("N", ("F.Cu",), (("F.Cu", .7),))))
        self.assertEqual(one(layer_rule(generated(output, "N"), "F.Cu"), "width"), ["width", "0.7"])
        ignored_grammar = text.replace('(rule (width .25)', '(layer_rule F.Cu (rule (width .6))) (rule (width .25)')
        with self.assertRaisesRegex(ValidationError, "structure layer_rule"):
            compile_electrical_dsn(ignored_grammar, profile(NetRule("N", ("F.Cu",))))

    def test_zero_typed_clearance_is_preserved(self):
        text = DSN.replace('280 (type smd_smd)', '0 (type smd_smd)')
        new = generated(compile_electrical_dsn(text, profile(NetRule("signal N", LAYERS))))
        self.assertEqual(children(layer_rule(new, "F.Cu"), "clearance")[1][1], "0")

    def test_global_pin_escape_clearance_stays_global(self):
        text = DSN.replace('(clearance 200)', '(clearance 200) (clearance 50 (type smd_to_turn_gap))')
        output = compile_electrical_dsn(text, profile(NetRule("signal N", LAYERS)))
        self.assertEqual(output.count('smd_to_turn_gap'), 1)
        self.assertIn(["clearance", "50", ["type", "smd_to_turn_gap"]],
                      one(one(parse(output), "structure"), "rule"))

    def test_profile_values_are_deeply_immutable_and_validated(self):
        rule = NetRule("Q", LAYERS)
        with self.assertRaises(FrozenInstanceError):
            rule.net = "N"
        invalid = (lambda: NetRule("Q", ["F.Cu"]), lambda: NetRule("Q", ("F.Cu", "F.Cu")),
                   lambda: NetRule('bad"net', LAYERS), lambda: NetRule("Q", LAYERS, (("F.Cu", float("nan")),)),
                   lambda: NetRule("Q", ("F.Cu",), (("B.Cu", .3),)),
                   lambda: NetRule("Q", LAYERS, (("F.Cu", .3), ("F.Cu", .4))),
                   lambda: NetRule("Q", LAYERS, clearance_mm=-1),
                   lambda: ElectricalRules([rule]), lambda: profile(rule, rule),
                   lambda: ElectricalRules(version=True), lambda: ElectricalRules(version=2))
        for make in invalid:
            with self.subTest(make=make), self.assertRaises(ValidationError):
                make()
        self.assertEqual(compile_electrical_dsn(DSN, ElectricalRules()), DSN)


class ElectricalPlanTests(unittest.TestCase):
    def test_clearance_is_unresolved_without_board_specific_evidence(self):
        policy = profile(NetRule("N", ("F.Cu",), clearance_mm=.4))
        plan = RoutePlan("x", (Track("N", "F.Cu", .4, ((0, 0), (1, 0))),), ())
        self.assertEqual(validate_plan_rules(plan, policy, LAYERS),
                         ("N: clearance 0.4 mm requires independent board-specific validation.",))

    def test_forbidden_track_and_wrong_width_rejected_independent_of_router(self):
        policy = profile(NetRule("N", ("F.Cu", "B.Cu"), (("F.Cu", .4),)))
        tracks = (Track("N", "In1.Cu", .4, ((0, 0), (1, 0))),
                  Track("N", "F.Cu", .3, ((0, 0), (1, 0))))
        issues = validate_plan_rules(RoutePlan("x", tracks, ()), policy, LAYERS)
        self.assertTrue(any("forbidden" in issue for issue in issues))
        self.assertTrue(any("width" in issue for issue in issues))

    def test_through_via_crossing_plane_and_outer_endpoint_needs_no_plane_trace(self):
        policy = profile(NetRule("N", ("F.Cu",), (("F.Cu", .4),)))
        plan = RoutePlan("x", (Track("N", "F.Cu", .4, ((0, 0), (1, 0))),),
                         (Via("N", (1, 0), ViaSpec(.6, .3, ("F.Cu", "B.Cu"))),))
        self.assertEqual(validate_plan_rules(plan, policy, LAYERS), ())

    def test_unknown_track_and_via_layers_invalid_width_and_profile_refused(self):
        plan = RoutePlan("x", (Track("N", "Ghost", float("nan"), ((0, 0), (1, 0))),),
                         (Via("N", (1, 0), ViaSpec(.6, .3, ("F.Cu", "Ghost"))),))
        self.assertEqual(len(validate_plan_rules(plan, ElectricalRules(), LAYERS)), 3)
        with self.assertRaises(ValidationError):
            validate_plan_rules(plan, profile(NetRule("N", ("Ghost",))), LAYERS)

    def test_unprofiled_nets_and_quantized_correct_width_are_accepted(self):
        plan = RoutePlan("x", (Track("Q", "In2.Cu", .2, ((0, 0), (1, 0))),
                               Track("N", "F.Cu", .4000001, ((0, 0), (1, 0)))), ())
        self.assertEqual(validate_plan_rules(plan, profile(NetRule("N", ("F.Cu",),
                                                                   (("F.Cu", .4),))), LAYERS), ())


if __name__ == "__main__":
    unittest.main()
