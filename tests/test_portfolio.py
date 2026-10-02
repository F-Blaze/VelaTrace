"""Portfolio selection and failure injection; no router, IPC or network required."""
from dataclasses import FrozenInstanceError, replace
import math
from types import SimpleNamespace
import unittest

from velatrace.errors import ValidationError
from velatrace.portfolio import (
    CALLBACK_LIMITATION, CandidateProducer, PortfolioBudget, PortfolioCancelled,
    candidate_geometry, explore_candidates,
)
from velatrace.routing import ValidationReport, plan_digest
from velatrace.ses import RoutePlan, Track, Via, ViaSpec


BOARD = "board-digest"
REQUIRED = frozenset({"width", "clearance"})
SPEC = ViaSpec(.6, .3, ("F.Cu", "B.Cu"))


def plan(length=1, *, y=0, vias=()):
    return RoutePlan("board", (Track("N", "F.Cu", .25, ((0, y), (length, y))),), vias)


def report(candidate, **changes):
    base = ValidationReport(plan_digest(candidate), 0, 0, board_digest=BOARD,
                            enforced_constraint_ids=REQUIRED)
    return replace(base, **changes)


class Clock:
    value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.calls = []
        self.evidence = None

    def validate(self, candidate):
        self.calls.append(candidate)
        result = report(candidate)
        self.evidence = (candidate, result)
        return result

    def run_search(self, values, **options):
        producers = [CandidateProducer(f"candidate-{index}", lambda remaining, p=p: p)
                     for index, p in enumerate(values, 1)]
        defaults = dict(budget=PortfolioBudget(10, 100), expected_board_digest=BOARD,
                        required_constraint_ids=REQUIRED, assert_fresh=lambda: None,
                        cancelled=lambda: False, clock=self.clock)
        defaults.update(options)
        return explore_candidates(producers, defaults.pop("validate", self.validate), **defaults)

    def test_winner_is_revalidated_and_restores_mutable_evidence(self):
        short, long = plan(1), plan(2)
        result = self.run_search([short, long])
        self.assertEqual(self.calls, [short, long, short])
        self.assertIs(result.selected_plan, short)
        self.assertIs(result.selected_report, self.evidence[1])
        self.assertEqual(result.selected_metrics.copper_length_mm, 1)
        self.assertEqual(result.stop_reason, "producers-exhausted")
        self.assertEqual([row.status for row in result.diagnostics], ["feasible", "feasible", "selected"])
        self.assertEqual(result.callback_limitations, (CALLBACK_LIMITATION,))

    def test_shorter_copper_precedes_via_count_and_vias_break_length_ties(self):
        vias = (Via("N", (0, 0), SPEC), Via("N", (1, 0), SPEC))
        short_two_vias, long_no_vias = plan(1, vias=vias), plan(2)
        result = self.run_search([short_two_vias, long_no_vias])
        self.assertIs(result.selected_plan, short_two_vias)
        short_one_via = plan(1, vias=vias[:1])
        result = self.run_search([short_two_vias, short_one_via])
        self.assertIs(result.selected_plan, short_one_via)

    def test_equal_quality_keeps_first_producer(self):
        a, b = plan(y=1), plan(y=2)
        result = self.run_search([a, b])
        self.assertIs(result.selected_plan, a)
        self.assertEqual(result.winner.attempt, 1)

    def test_only_feasible_complete_bound_reports_can_win(self):
        cases = (
            {"plan_digest": "other"}, {"board_digest": "other"},
            {"drc_violations": None}, {"drc_violations": 1},
            {"unconnected_count": None}, {"unconnected_count": 1},
            {"blocking_reasons": ("electrical constraint unsupported",)},
            {"enforced_constraint_ids": frozenset({"width"})},
            {"enforced_constraint_ids": REQUIRED | {"unconfirmed"}},
            {"routed_connections": 1, "total_connections": 2},
            {"routed_connections": 1},
        )
        short, good = plan(1), plan(2)
        for changes in cases:
            with self.subTest(changes=changes):
                def validate(candidate):
                    return report(candidate, **changes) if candidate is short else report(candidate)
                result = self.run_search([short, good], validate=validate)
                self.assertIs(result.selected_plan, good)
                self.assertEqual(result.diagnostics[0].status, "infeasible")

    def test_unknown_or_duck_typed_report_cannot_win(self):
        for value in (None, False, SimpleNamespace(drc_violations=0, unconnected_count=0)):
            with self.subTest(value=value):
                result = self.run_search([plan()], validate=lambda p: value)
                self.assertIsNone(result.winner)
                self.assertEqual(result.diagnostics[0].status, "infeasible")

    def test_preexisting_warnings_do_not_override_zero_blocking_drc(self):
        result = self.run_search([plan()], validate=lambda p: report(p, preexisting_warnings=3))
        self.assertIsNotNone(result.winner)
        self.assertEqual(result.selected_report.preexisting_warnings, 3)

    def test_producer_and_validation_failures_are_diagnostics_not_winners(self):
        def fail(_):
            raise RuntimeError("failure")
        producers = [CandidateProducer("broken", fail), CandidateProducer("good", lambda _: plan())]
        result = explore_candidates(producers, self.validate, budget=PortfolioBudget(3, 100),
                                    expected_board_digest=BOARD, required_constraint_ids=REQUIRED,
                                    assert_fresh=lambda: None, cancelled=lambda: False, clock=self.clock)
        self.assertEqual(result.diagnostics[0].status, "producer-failed")
        self.assertIsNotNone(result.winner)
        result = self.run_search([plan()], validate=fail)
        self.assertEqual(result.diagnostics[0].status, "validation-failed")
        self.assertIsNone(result.winner)

    def test_malformed_plan_cannot_reach_validator(self):
        malformed = [None, replace(plan(), tracks=list(plan().tracks)), plan(0), plan(float("nan"))]
        result = self.run_search(malformed)
        self.assertIsNone(result.winner)
        self.assertFalse(self.calls)
        self.assertTrue(all(row.status == "invalid-plan" for row in result.diagnostics))

    def test_geometric_duplicates_do_not_consume_validation(self):
        a = plan(2)
        duplicate = RoutePlan("board", (
            Track("N", "F.Cu", .2500001, ((2.0000001, 0), (1, 0), (.0000001, 0))),
            Track("N", "F.Cu", .25, ((0, 0), (2, 0))),
        ), ())
        result = self.run_search([a, duplicate])
        self.assertEqual(self.calls, [a, a])
        self.assertEqual(result.diagnostics[1].status, "duplicate")

    def test_candidate_budget_counts_duplicate_and_failed_attempts(self):
        a, b = plan(), plan(2)
        result = self.run_search([a, a, b], budget=PortfolioBudget(2, 100))
        self.assertEqual(self.calls, [a, a])
        self.assertEqual(result.stop_reason, "candidate-limit")
        self.assertIs(result.selected_plan, a)

    def test_empty_producers_return_no_winner(self):
        result = self.run_search([])
        self.assertIsNone(result.winner)
        self.assertFalse(self.calls)
        self.assertEqual(result.stop_reason, "producers-exhausted")

    def test_freshness_surrounds_producer_and_validator(self):
        events = []
        def fresh():
            events.append("fresh")
        def produce(_):
            events.append("produce")
            return plan()
        def validate(candidate):
            events.append("validate")
            return report(candidate)
        explore_candidates([CandidateProducer("one", produce)], validate,
                           budget=PortfolioBudget(1, 10), expected_board_digest=BOARD,
                           required_constraint_ids=REQUIRED, assert_fresh=fresh,
                           cancelled=lambda: False, clock=self.clock)
        for index, event in enumerate(events):
            if event in {"produce", "validate"}:
                self.assertEqual(events[index - 1], "fresh")
                self.assertEqual(events[index + 1], "fresh")

    def test_stale_state_after_failing_producer_is_not_swallowed(self):
        state = {"stale": False}
        def fresh():
            if state["stale"]:
                raise ValidationError("board changed")
        def produce(_):
            state["stale"] = True
            raise RuntimeError("router failed at same time")
        with self.assertRaisesRegex(ValidationError, "board changed"):
            explore_candidates([CandidateProducer("one", produce)], self.validate,
                               budget=PortfolioBudget(1, 10), expected_board_digest=BOARD,
                               required_constraint_ids=REQUIRED, assert_fresh=fresh,
                               cancelled=lambda: False, clock=self.clock)
        self.assertFalse(self.calls)

    def test_stale_state_during_validation_discards_prior_best(self):
        state = {"stale": False}
        good, stale = plan(), plan(2)
        def fresh():
            if state["stale"]:
                raise ValidationError("rules changed")
        def validate(candidate):
            state["stale"] = candidate is stale
            return report(candidate)
        with self.assertRaisesRegex(ValidationError, "rules changed"):
            self.run_search([good, stale], validate=validate, assert_fresh=fresh)

    def test_cancel_before_or_after_expensive_call_never_returns_best(self):
        with self.assertRaises(PortfolioCancelled):
            self.run_search([plan()], cancelled=lambda: True)
        self.assertFalse(self.calls)
        state = {"cancelled": False}
        def validate(candidate):
            state["cancelled"] = True
            return report(candidate)
        with self.assertRaises(PortfolioCancelled):
            self.run_search([plan()], validate=validate, cancelled=lambda: state["cancelled"])

    def test_explicit_callback_cancellation_is_not_a_candidate_failure(self):
        def validate(candidate):
            raise PortfolioCancelled("cancelled by validation")
        with self.assertRaises(PortfolioCancelled):
            self.run_search([plan()], validate=validate)

    def test_cancellation_during_final_revalidation_invalidates_search(self):
        state = {"calls": 0, "cancelled": False}
        def validate(candidate):
            state["calls"] += 1
            state["cancelled"] = state["calls"] == 2
            return report(candidate)
        with self.assertRaises(PortfolioCancelled):
            self.run_search([plan()], validate=validate, cancelled=lambda: state["cancelled"])
        self.assertEqual(state["calls"], 2)

    def test_time_budget_is_passed_to_producer_and_overrun_is_rejected(self):
        remaining_values = []
        def produce(remaining):
            remaining_values.append(remaining)
            self.clock.advance(11)
            return plan()
        result = explore_candidates([CandidateProducer("slow", produce)], self.validate,
                                    budget=PortfolioBudget(1, 10), expected_board_digest=BOARD,
                                    required_constraint_ids=REQUIRED, assert_fresh=lambda: None,
                                    cancelled=lambda: False, clock=self.clock)
        self.assertEqual(remaining_values, [10])
        self.assertFalse(self.calls)
        self.assertIsNone(result.winner)
        self.assertEqual(result.elapsed_seconds, 11)
        self.assertEqual(result.stop_reason, "time-limit")
        self.assertIn(CALLBACK_LIMITATION, result.diagnostics[0].details)

    def test_late_validation_result_cannot_win(self):
        def validate(candidate):
            self.clock.advance(10)
            return report(candidate)
        result = self.run_search([plan()], validate=validate, budget=PortfolioBudget(1, 10))
        self.assertIsNone(result.winner)
        self.assertFalse(result.candidates)
        self.assertEqual(result.stop_reason, "time-limit")

    def test_final_revalidation_is_inside_budget_and_cannot_return_late_winner(self):
        def validate(candidate):
            self.clock.advance(6)
            return report(candidate)
        result = self.run_search([plan()], validate=validate, budget=PortfolioBudget(1, 10))
        self.assertIsNone(result.winner)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.diagnostics[-1].status, "final-time-limit")

    def test_failed_final_validation_does_not_fall_back_to_stale_report(self):
        count = 0
        def validate(candidate):
            nonlocal count
            count += 1
            return report(candidate, unconnected_count=1 if count == 2 else 0)
        result = self.run_search([plan()], validate=validate)
        self.assertIsNone(result.winner)
        self.assertEqual(result.stop_reason, "final-validation-failed")
        self.assertEqual(result.diagnostics[-1].status, "final-validation-failed")

    def test_budget_values_and_result_are_immutable(self):
        for candidates, seconds in ((0, 1), (True, 1), (1, 0), (1, float("inf")), (1, True)):
            with self.subTest(candidates=candidates, seconds=seconds), self.assertRaises(ValidationError):
                PortfolioBudget(candidates, seconds)
        result = self.run_search([plan()])
        with self.assertRaises(FrozenInstanceError):
            result.stop_reason = "changed"


class GeometryTests(unittest.TestCase):
    def test_collinear_splitting_overlaps_and_direction_have_identical_key(self):
        a = RoutePlan("board", (Track("N", "F.Cu", .25, ((0, 0), (2, 2))),), ())
        b = RoutePlan("board", (
            Track("N", "F.Cu", .25, ((2, 2), (1, 1))),
            Track("N", "F.Cu", .25, ((0, 0), (1.5, 1.5))),
        ), ())
        key_a, metrics_a = candidate_geometry(a)
        key_b, metrics_b = candidate_geometry(b)
        self.assertEqual(key_a, key_b)
        self.assertEqual(metrics_a, metrics_b)
        self.assertAlmostEqual(metrics_a.copper_length_mm, math.sqrt(8))

    def test_net_layer_width_position_and_via_dimensions_are_not_deduplicated(self):
        a = plan()
        variants = [
            replace(a, tracks=(replace(a.tracks[0], net="OTHER"),)),
            replace(a, tracks=(replace(a.tracks[0], layer="B.Cu"),)),
            replace(a, tracks=(replace(a.tracks[0], width_mm=.3),)),
            plan(y=.000001),
        ]
        for variant in variants:
            self.assertNotEqual(candidate_geometry(a)[0], candidate_geometry(variant)[0])
        via_a = Via("N", (0, 0), SPEC)
        via_b = replace(via_a, spec=ViaSpec(.7, .3, SPEC.layers))
        self.assertNotEqual(candidate_geometry(plan(vias=(via_a,)))[0],
                            candidate_geometry(plan(vias=(via_b,)))[0])

    def test_quantized_via_repeats_have_one_physical_count(self):
        a = Via("N", (0, 0), SPEC)
        b = Via("N", (.0000001, 0), SPEC)
        key, metrics = candidate_geometry(plan(vias=(a, b)))
        self.assertEqual(metrics.via_count, 1)
        self.assertEqual(key, candidate_geometry(plan(vias=(a,)))[0])


if __name__ == "__main__":
    unittest.main()
