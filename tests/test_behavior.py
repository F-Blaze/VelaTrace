"""Fixture connectivity, arithmetic, ambiguity and refusal regression coverage."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from velatrace.constraints import ConstraintStore
from velatrace.dsn import DsnInput, ExportTicket, file_digest
from velatrace.errors import ValidationError
from velatrace.flags import (Bucket, Flag, Function, Verdict, candidate_savings,
                             compute_flags, flagged_total, redundancy_flags)
from velatrace.models import Pin
from velatrace.netlist import read_xml_netlist
from velatrace.routing import RoutingSession, ValidationReport
from velatrace.sexpr import children, one, parse
from velatrace.tokens import Prompt, TokenEstimate

FIXTURES = Path(__file__).parent / 'fixtures' / 'audit'


def board_and_netlist():
    """Test-only position join; assert the independently authored files agree."""
    snapshot = read_xml_netlist(FIXTURES / 'necessity.xml')
    board = parse((FIXTURES / 'necessity.kicad_pcb').read_text(encoding='utf-8'))
    by_ref = {}
    for footprint in children(board, 'footprint'):
        properties = {row[1]: row[2] for row in children(footprint, 'property')}
        by_ref[properties['Reference']] = footprint
    assert set(by_ref) == {c.reference for c in snapshot.components}
    components = []
    for comp in snapshot.components:
        footprint = by_ref[comp.reference]
        pad_nets = {(pad[1], one(pad, 'net')[2]) for pad in children(footprint, 'pad')}
        assert pad_nets == {(pin.number, pin.net) for pin in comp.pins}
        assert footprint[1] == comp.footprint
        at = one(footprint, 'at')
        components.append(replace(comp, position_mm=(float(at[1]), float(at[2]))))
    return tuple(components)


class FixtureFlagTests(unittest.TestCase):
    def setUp(self):
        self.components = board_and_netlist()
        self.functions = {
            'C1': Function('Stores rail energy', 'bulk capacitor', .99),
            'C2': Function('Decouples local supply', 'decoupling capacitor', .99),
            'U1': Function('Measures rail temperature', 'rail temperature sensor', .99),
            'U2': Function('Measures rail temperature', 'rail temperature sensor', .99),
        }
        self.sensors = self.components[2:]

    def test_fixture_bulk_decoupling_excluded_duplicate_sensor_flagged(self):
        flags = redundancy_flags(self.components, self.functions)
        self.assertEqual(len(flags), 1)
        self.assertEqual((flags[0].reference, flags[0].duplicate_of, flags[0].possible),
                         ('U2', 'U1', False))

    def test_schematic_only_duplicate_is_possible_without_board_proximity(self):
        snapshot = read_xml_netlist(FIXTURES / 'necessity.xml')
        flags = redundancy_flags(snapshot.components, self.functions)
        self.assertEqual([flag.reference for flag in flags], ['U2'])
        self.assertTrue(flags[0].possible)
        self.assertEqual(flags[0].reason, 'possible redundancy — verify')

    def test_same_nets_with_different_pin_function_mapping_are_not_duplicates(self):
        first, second = self.sensors
        second = replace(second, pins=(Pin('1', 'GND'), Pin('2', 'VCC'), Pin('3', 'TEMP')))
        self.assertEqual(first.nets, second.nets)
        self.assertEqual(redundancy_flags((first, second), self.functions), [])

    def test_value_footprint_type_and_connected_pin_facts_required(self):
        first, second = self.sensors
        alternatives = [replace(second, value='OTHER'), replace(second, footprint='OTHER'),
                        replace(second, kind='different'), replace(second, value=''),
                        replace(second, footprint=''), replace(second, pins=()),
                        replace(second, pins=(Pin('1', ''),))]
        for changed in alternatives:
            with self.subTest(component=changed):
                self.assertEqual(redundancy_flags((first, changed), self.functions), [])

    def test_distance_confidence_and_role_disagreement_remain_possible(self):
        first, second = self.sensors
        cases = [((first, replace(second, position_mm=(200, 10))), self.functions),
                 ((first, replace(second, position_mm=None)), self.functions),
                 (self.sensors, {**self.functions, 'U2': Function('Backup', 'backup sensor', .99)}),
                 (self.sensors, {**self.functions, 'U2': Function('Maybe', 'rail temperature sensor', .79)}),
                 (self.sensors, {**self.functions, 'U2': Function('Unknown', 'unknown', 1)}),
                 (self.sensors, {})]
        for comps, functions in cases:
            with self.subTest(functions=functions):
                flags = redundancy_flags(comps, functions)
                self.assertEqual(len(flags), 1)
                self.assertTrue(flags[0].possible)
                self.assertEqual(candidate_savings(flags, {'U2': Decimal('2')}), Decimal('0'))

    def test_proximity_boundary_and_invalid_values(self):
        first, second = self.sensors
        second = replace(second, position_mm=(first.position_mm[0] + 5, first.position_mm[1]))
        self.assertFalse(redundancy_flags((first, second), self.functions)[0].possible)
        for bad in (-1, 0, True, '5', float('nan'), float('inf')):
            with self.subTest(proximity=bad), self.assertRaises(ValidationError):
                redundancy_flags(self.sensors, self.functions, bad)

    def test_model_redundant_assertion_alone_never_creates_flag(self):
        comp = self.components[0]
        self.assertEqual(compute_flags((comp,), self.functions,
                         {comp.reference: Verdict(comp.reference, Bucket.REDUNDANT, .99, 'Remove')}), [])

    def test_important_conflict_and_borderline_rules(self):
        verdicts = {comp.reference: Verdict(comp.reference, Bucket.IMPORTANT, .99, 'Keep')
                    for comp in self.components}
        verdicts['C1'] = replace(verdicts['C1'], confidence=.79)
        verdicts['C2'] = replace(verdicts['C2'], bucket=Bucket.NICE_TO_HAVE)
        flags = {f.reference: f for f in compute_flags(self.components, self.functions, verdicts)}
        self.assertTrue(flags['U2'].possible)
        self.assertTrue(flags['C1'].possible)
        self.assertFalse(flags['C2'].possible)
        self.assertEqual(candidate_savings(list(flags.values()),
                         {'U2': Decimal('2'), 'C1': Decimal('1.5'), 'C2': Decimal('.1')}), Decimal('.1'))

    def test_low_classification_confidence_on_either_duplicate_excludes_savings(self):
        verdicts = {'U1': Verdict('U1', Bucket.CRITICAL, .99, 'Keep the original sensor'),
                    'U2': Verdict('U2', Bucket.REDUNDANT, .99, 'Verify duplicate function')}
        for uncertain_reference in ('U1', 'U2'):
            with self.subTest(uncertain_reference=uncertain_reference):
                uncertain = {**verdicts, uncertain_reference:
                             replace(verdicts[uncertain_reference], confidence=.79)}
                flags = compute_flags(self.sensors, self.functions, uncertain)
                duplicate = next(flag for flag in flags if flag.reference == 'U2')
                self.assertEqual(duplicate.reason, 'possible redundancy — verify')
                self.assertTrue(duplicate.possible)
                self.assertEqual(candidate_savings(flags, {'U2': Decimal('2')}), Decimal('0'))
        boundary = {ref: replace(verdict, confidence=.8) for ref, verdict in verdicts.items()}
        flags = compute_flags(self.sensors, self.functions, boundary)
        self.assertFalse(flags[0].possible)
        self.assertEqual(candidate_savings(flags, {'U2': Decimal('2')}), Decimal('2'))

    def test_low_confidence_optional_component_excludes_savings(self):
        component = self.components[0]
        verdict = Verdict(component.reference, Bucket.NICE_TO_HAVE, .79, 'Possibly optional')
        flags = compute_flags((component,), self.functions, {component.reference: verdict})
        self.assertEqual(len(flags), 1)
        self.assertTrue(flags[0].possible)
        self.assertEqual(flags[0].reason, 'Borderline classification — verify')
        self.assertEqual(candidate_savings(flags, {component.reference: Decimal('1.5')}), Decimal('0'))
        flags = compute_flags((component,), self.functions,
                              {component.reference: replace(verdict, confidence=.8)})
        self.assertFalse(flags[0].possible)
        self.assertEqual(candidate_savings(flags, {component.reference: Decimal('1.5')}), Decimal('1.5'))


class ArithmeticTests(unittest.TestCase):
    def test_precise_decimal_totals_empty_and_unique_savings(self):
        self.assertEqual(flagged_total({}), Decimal(0))
        self.assertEqual(flagged_total({'A': Decimal('.10'), 'B': Decimal('.20')}), Decimal('.30'))
        flags = [Flag('A', 'optional'), Flag('A', 'optional'), Flag('B', 'possible', True),
                 Flag('missing', 'optional')]
        self.assertEqual(candidate_savings(flags, {'A': Decimal('.1'), 'B': Decimal('500')}), Decimal('.1'))
        for bad in ('-1', 'NaN', 'Infinity', '-Infinity'):
            with self.subTest(amount=bad), self.assertRaises(ValidationError):
                flagged_total({'A': Decimal(bad)})

    def test_token_cost_includes_every_retry_and_rejects_invalid_counts(self):
        estimate = TokenEstimate(1234, 456, 'fingerprint', 'fixture', calls_max=3)
        self.assertEqual(estimate.cost_ceiling(Decimal('.2'), Decimal('.6')), Decimal('.0015612'))
        for fields in ((-1, 1, 1), (True, 1, 1), (1.2, 1, 1), (1, 0, 1),
                       (1, -1, 1), (1, True, 1), (1, 1, 0), (1, 1, -1), (1, 1, 1.5)):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                TokenEstimate(fields[0], fields[1], 'fp', 'fixture', fields[2])
        for bad in ('NaN', 'Infinity', '-1'):
            with self.subTest(price=bad), self.assertRaises(ValidationError):
                estimate.cost_ceiling(Decimal(bad), Decimal(0))

    def test_prompt_fingerprint_binds_actual_unicode_and_roles(self):
        prompt = Prompt('system', '抵抗器 R1')
        self.assertEqual(prompt.fingerprint, Prompt('system', '抵抗器 R1').fingerprint)
        self.assertNotEqual(prompt.fingerprint, Prompt('抵抗器 R1', 'system').fingerprint)
        self.assertNotEqual(prompt.fingerprint, Prompt('system', '抵抗器 R2').fingerprint)

    def test_report_percentage_uses_verified_counts_and_handles_unknown(self):
        self.assertEqual(ValidationReport('x', 0, 13, 87, 100).percent_routed, 87)
        self.assertIsNone(ValidationReport('x', None, None).percent_routed)
        self.assertIsNone(ValidationReport('x', 0, 0, 0, 0).percent_routed)
        for values in ((True, 0, None, None), (0, -1, None, None), (0, 0, 101, 100)):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                ValidationReport('x', *values)


class RefusalTests(unittest.TestCase):
    def test_malformed_ses_stops_before_candidate_validation_and_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            board = path / 'board.kicad_pcb'
            board.write_text((FIXTURES / 'necessity.kicad_pcb').read_text(encoding='utf-8'), encoding='utf-8')
            dsn = path / 'board.dsn'
            dsn.write_text('test-only unchanged DSN ticket')
            router = SimpleNamespace(check_startup=lambda: None, route=lambda *_: '(session malformed')
            validator = Mock()
            validator.supports.return_value = True
            writer = Mock()
            session = RoutingSession(ConstraintStore(path / 'constraints.json'), router, validator)
            session.command('/autoroute')
            session.set_input(DsnInput(dsn, file_digest(dsn), ExportTicket.begin(board),
                              frozenset({'VCC', 'GND'}), frozenset({'F.Cu', 'B.Cu'})),
                              all_footprints_placed=True)
            session.confirm_constraints(session.constraints.fingerprint)
            original = board.read_bytes()
            with self.assertRaises(ValidationError):
                session.run()
            with self.assertRaises(ValidationError):
                session.approve(writer)
            validator.validate.assert_not_called()
            writer.apply.assert_not_called()
            self.assertEqual(board.read_bytes(), original)

    def test_conflicting_xml_pin_membership_refused(self):
        original = (FIXTURES / 'necessity.xml').read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'malformed.xml'
            for fragment in ('<node ref="C1" pin="1" />', '<node ref="MISSING" pin="1" />'):
                path.write_text(original.replace('</nets>', f'<net code="9" name="OTHER">{fragment}</net></nets>'), encoding='utf-8')
                with self.assertRaises(ValidationError):
                    read_xml_netlist(path)


if __name__ == '__main__':
    unittest.main()
