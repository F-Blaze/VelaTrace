"""Conservative, disposable DRC-guided removal of Freerouting dead-end stubs.

This helper only proposes a cleaned RoutePlan. The caller must validate the
returned plan again before using it for preview or approval.
"""
from collections import Counter
from dataclasses import dataclass
import math
import time

from .candidate import SafeCandidateValidator, candidate_text, context_matches
from .errors import ValidationError
from .routing import plan_digest
from .ses import RoutePlan, Track


def _q(value):
    return round(value * 1_000_000) / 1_000_000


@dataclass(frozen=True)
class RepairResult:
    plan: RoutePlan
    removed_segments: int
    passes: int
    details: tuple[str, ...] = ()


def _segment_sources(plan: RoutePlan, items):
    """Map temporary item UUIDs to exact RoutePlan edges, checking order/shape."""
    expected = [(track_index, edge_index)
                for track_index, track in enumerate(plan.tracks)
                for edge_index in range(len(track.points_mm) - 1)]
    segment_count = len(expected)
    via_count = len(plan.vias)
    if len(items) != segment_count + via_count:
        raise ValidationError("Candidate evidence does not match the route geometry.")
    mapping = {}
    for index, (track_index, edge_index) in enumerate(expected):
        item = items[index]
        track = plan.tracks[track_index]
        start, end = track.points_mm[edge_index:edge_index + 2]
        expected_start = (_q(start[0]), _q(-start[1]))
        expected_end = (_q(end[0]), _q(-end[1]))
        if (item.kind != "segment" or not item.id or item.id in mapping
                or item.net != track.net or item.layer != track.layer
                or item.start != expected_start or item.end != expected_end
                or item.width != _q(track.width_mm)):
            raise ValidationError("Candidate segment UUID mapping is ambiguous.")
        mapping[item.id] = (track_index, edge_index)
    seen_ids = set(mapping)
    for item, via in zip(items[segment_count:], plan.vias):
        expected_position = (_q(via.position_mm[0]), _q(-via.position_mm[1]))
        if (item.kind != "via" or not item.id or item.id in mapping
                or item.net != via.net or item.start != expected_position
                or item.width != _q(via.spec.diameter_mm)
                or item.drill != _q(via.spec.drill_mm)):
            raise ValidationError("Candidate via UUID mapping is ambiguous.")
        if item.id in seen_ids:
            raise ValidationError("Candidate copper UUID mapping is ambiguous.")
        seen_ids.add(item.id)
    return mapping


def _without_edges(plan: RoutePlan, sources):
    tracks = []
    for track_index, track in enumerate(plan.tracks):
        run = [track.points_mm[0]]
        for edge_index, (_, end) in enumerate(zip(track.points_mm, track.points_mm[1:])):
            if (track_index, edge_index) in sources:
                if len(run) > 1:
                    tracks.append(Track(track.net, track.layer, track.width_mm, tuple(run)))
                run = [end]
            else:
                run.append(end)
        if len(run) > 1:
            tracks.append(Track(track.net, track.layer, track.width_mm, tuple(run)))
    return RoutePlan(plan.base_design, tuple(tracks), plan.vias)


def _dangling_segment_ids(result, segment_ids):
    """Only single-item track_dangling issues identify a removable route segment."""
    targets = set()
    for kind, _severity, uuids in result.issues:
        if kind == "track_dangling" and len(uuids) == 1 and uuids[0] in segment_ids:
            targets.add(uuids[0])
    return targets


def _known_issues(result):
    return (len(result.issues) == result.violations + result.schematic_parity)


def _evaluate(validator, dsn, source, files, name, items, clearances, snapshot, context, deadline):
    """Run official candidate DRCs with stable temporary item UUIDs."""
    results = []
    for clearance in clearances:
        validator.safety.assert_matches(dsn, expected_board=snapshot)
        dsn.assert_unchanged()
        if not context_matches(context):
            raise ValidationError("Project/rules changed during DRC; route again.")
        if time.monotonic() >= deadline:
            raise TimeoutError("Repair time budget expired.")
        rules = validator._with_rule(files, name, clearance)
        result = validator._drc(name, candidate_text(source, items), rules, False)
        # The CLI call is synchronous; reject any result that completed outside
        # the requested budget, then check all source snapshots again.
        dsn.assert_unchanged()
        validator.safety.assert_matches(dsn, expected_board=snapshot)
        if not context_matches(context):
            raise ValidationError("Project/rules changed during DRC; route again.")
        if time.monotonic() >= deadline:
            raise TimeoutError("Repair time budget expired.")
        if not _known_issues(result):
            raise ValidationError("KiCad did not return complete DRC issue identities.")
        results.append(result)
    return tuple(results)


def repair_dangling(plan: RoutePlan, dsn, constraints, validator: SafeCandidateValidator, *,
                    max_passes: int = 4, timeout_seconds: float = 30) -> RepairResult:
    """Remove only DRC-identified dead-end segments from disposable candidates.

    The original plan is returned on any inconclusive trial. Stale board/project
    state propagates as a validation error. This function never writes a board.
    Callers must run the normal validator on the returned plan before use.
    """
    if (type(max_passes) is not int or not 1 <= max_passes <= 16
            or type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120):
        raise ValidationError("Invalid DRC cleanup budget.")
    original = plan
    removed = set()
    source_map = {}
    passes = 0
    details = []
    validator.evidence = None
    # Stale inputs are an error, never a no-op repair result.
    dsn.assert_unchanged()
    source = validator.safety.assert_matches(dsn)
    try:
        started = time.monotonic()
        deadline = started + timeout_seconds
        initial = validator.validate(dsn, plan, tuple(constraints))
        dsn.assert_unchanged()
        if validator.safety.assert_matches(dsn) != source:
            raise ValidationError("Board changed during DRC; route again.")
        evidence = validator.evidence
        expected_constraints = frozenset(item.id for item in constraints)
        if (evidence is None or len(evidence) != 6 or evidence[:3] !=
                (dsn.digest, plan_digest(plan), initial)
                or initial.plan_digest != plan_digest(plan)
                or initial.board_digest != dsn.ticket.board_digest
                or initial.enforced_constraint_ids != expected_constraints):
            return RepairResult(original, 0, 0, ("Cleanup skipped: candidate evidence was incomplete.",))
        _, _, _, original_items, context, snapshot = evidence
        if (validator.safety.assert_matches(dsn, expected_board=snapshot) != source
                or not context_matches(context)):
            raise ValidationError("Board or project/rules changed during DRC; route again.")
        if (type(initial.drc_violations) is int and initial.drc_violations == 0
                and type(initial.unconnected_count) is int and initial.unconnected_count == 0
                and not initial.blocking_reasons):
            return RepairResult(original, 0, 0, ("Initial independent validation is already clean.",))
        source_map = _segment_sources(plan, original_items)
        segment_ids = set(source_map)
        if time.monotonic() >= deadline:
            return RepairResult(original, 0, 0, ("Cleanup skipped: DRC cleanup time budget expired.",))
        files = validator._context_files(context)
        name = dsn.ticket.board_path.name
        current_items = tuple(original_items)
        current_plan = plan
        clearances = validator._rule_sets(tuple(constraints))

        def fresh():
            dsn.assert_unchanged()
            live = validator.safety.assert_matches(dsn, expected_board=snapshot)
            if live != source or not context_matches(context):
                raise ValidationError("Board or project/rules changed during DRC; route again.")

        fresh()
        current_results = _evaluate(validator, dsn, source, files, name, current_items,
                                    clearances, snapshot, context, deadline)
        if initial.unconnected_count != 0:
            return RepairResult(original, 0, 0,
                                ("Cleanup skipped: the original route is not fully connected.",))
        for result in current_results:
            if result.unconnected != 0:
                return RepairResult(original, 0, 0,
                                    ("Cleanup skipped: the original route is not fully connected.",))
        dangling = set.intersection(*(_dangling_segment_ids(result, segment_ids)
                                      for result in current_results)) if current_results else set()
        if not dangling:
            return RepairResult(original, 0, 0, ("No removable route-segment dangling issues found.",))

        for _ in range(max_passes):
            fresh()
            if time.monotonic() >= deadline:
                details.append("Cleanup stopped: DRC cleanup time budget expired.")
                break
            # Source IDs for already removed items are absent from current_items.
            remaining_ids = {item.id for item in current_items if item.kind == "segment"}
            dangling_sets = [_dangling_segment_ids(result, remaining_ids)
                             for result in current_results]
            dangling = set.intersection(*dangling_sets) if dangling_sets else set()
            if not dangling:
                break
            trial_removed = removed | dangling
            trial_items = tuple(item for item in original_items if item.id not in trial_removed)
            trial_results = _evaluate(validator, dsn, source, files, name, trial_items,
                                      clearances, snapshot, context, deadline)
            accepted = True
            progress = False
            for before, after in zip(current_results, trial_results):
                if after.unconnected != 0 or Counter(after.issues) - Counter(before.issues):
                    accepted = False
                    break
                before_targets = _dangling_segment_ids(before, remaining_ids)
                after_targets = _dangling_segment_ids(after, remaining_ids)
                if after_targets & dangling:
                    accepted = False
                    break
                if not before_targets - after_targets:
                    accepted = False
                    break
                if after_targets < before_targets:
                    progress = True
            if not accepted or not progress:
                details.append("Cleanup proposal rejected: it did not preserve full connectivity and DRC issues.")
                break
            removed = trial_removed
            current_items = trial_items
            current_results = trial_results
            current_plan = _without_edges(plan, {source_map[identifier] for identifier in removed})
            passes += 1
        if not removed:
            if not details:
                details.append("No safe dangling-segment cleanup was found.")
            return RepairResult(original, 0, passes, tuple(details))
        if time.monotonic() >= deadline:
            fresh()
            return RepairResult(original, 0, passes,
                                ("Cleanup rolled back: DRC cleanup time budget expired.",))

        fresh()
        final = validator.validate(dsn, current_plan, tuple(constraints))
        final_evidence = validator.evidence
        fresh()
        if time.monotonic() >= deadline:
            return RepairResult(original, 0, passes,
                                ("Cleanup rolled back: DRC cleanup time budget expired.",))
        if (final.plan_digest != plan_digest(current_plan)
                or final.board_digest != dsn.ticket.board_digest
                or final.enforced_constraint_ids != expected_constraints
                or type(final.drc_violations) is not int or final.drc_violations != 0
                or type(final.unconnected_count) is not int or final.unconnected_count != 0
                or final.blocking_reasons
                or final_evidence is None
                or len(final_evidence) != 6
                or final_evidence[:3] != (dsn.digest, plan_digest(current_plan), final)
                or final_evidence[4] != context or final_evidence[5] != snapshot):
            return RepairResult(original, 0, passes,
                                ("Cleanup rolled back: final independent validation did not prove a clean connected plan.",))
        if time.monotonic() >= deadline:
            return RepairResult(original, 0, passes,
                                ("Cleanup rolled back: DRC cleanup time budget expired.",))
        details.append(f"Removed {len(removed)} DRC-identified dangling segment(s); final independent DRC passed.")
        return RepairResult(current_plan, len(removed), passes, tuple(details))
    except TimeoutError:
        dsn.assert_unchanged()
        if 'snapshot' in locals():
            validator.safety.assert_matches(dsn, expected_board=snapshot)
        else:
            validator.safety.assert_matches(dsn)
        if 'context' in locals() and not context_matches(context):
            raise ValidationError("Board or project/rules changed during DRC; route again.")
        return RepairResult(original, 0, passes, ("Cleanup stopped: DRC cleanup time budget expired.",))
    except ValidationError:
        validator.evidence = None
        raise
    except Exception as exc:
        # Re-check freshness before converting ordinary tool failures into a
        # conservative no-op. A stale board/project must never look like success.
        dsn.assert_unchanged()
        live = (validator.safety.assert_matches(dsn, expected_board=snapshot)
                if 'snapshot' in locals() else validator.safety.assert_matches(dsn))
        if live != source or ('context' in locals() and not context_matches(context)):
            raise ValidationError("Board or project/rules changed during DRC; route again.") from exc
        return RepairResult(original, 0, passes,
                            (f"Cleanup unavailable: {type(exc).__name__}: {exc}",))
    finally:
        # Caller always performs the ordinary validation for the returned plan.
        validator.evidence = None
