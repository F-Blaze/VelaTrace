"""A faster failure must never become a routing speed win."""
import unittest

from velatrace.reference_benchmark import reference_comparison, run_reference_benchmark
from velatrace.errors import ValidationError


def case(trial, old_seconds=10, new_seconds=5, accepted=True):
    return {'layers': 4, 'trial': trial, 'solvers': {
        'baseline': {'accepted': False, 'total_seconds': 1},
        'reference-triangles': {'accepted': True, 'total_seconds': old_seconds},
        'vela-routing': {'accepted': accepted, 'total_seconds': new_seconds}}}


class ReferenceComparisonTests(unittest.TestCase):
    def test_single_success_cannot_support_repeated_speed_claim(self):
        result = reference_comparison([case(1)])['4']
        self.assertIsNone(result['projection_speedup'])
        self.assertEqual(result['baseline']['accepted'], 0)
        self.assertFalse(result['universal_superiority_proven'])

    def test_failed_or_missing_successes_are_not_filtered_into_speed_wins(self):
        result = reference_comparison([case(1), case(2), case(3, new_seconds=.1, accepted=False)])['4']
        self.assertEqual(result['vela-routing']['attempts'], 3)
        self.assertEqual(result['vela-routing']['accepted'], 2)
        self.assertIsNone(result['projection_speedup'])

    def test_reports_paired_median_and_all_attempt_times(self):
        result = reference_comparison([case(1), case(2), case(3, old_seconds=200, new_seconds=100)])['4']
        self.assertEqual(result['projection_speedup'], 2)
        self.assertEqual(result['vela-routing']['median_total_seconds'], 5)
        self.assertFalse(result['universal_superiority_proven'])

    def test_invalid_repeat_count_refused_before_output_or_tool_startup(self):
        for repeat in (0, 10, True, 1.5):
            with self.assertRaisesRegex(ValidationError, 'repeats'):
                run_reference_benchmark('unused', jar=None, java=None, kicad_cli=None, repeats=repeat)


if __name__ == '__main__':
    unittest.main()
