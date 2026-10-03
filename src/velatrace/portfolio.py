"""Bounded, sequential candidate search; never calls a live board writer.

The objective is lexicographic: shortest quantized copper centerline, then fewest
unique vias, then earliest producer. Only independently validated complete plans
compete. Length is a geometric metric, not an electrical delay measurement.

Callbacks are synchronous. The deadline prevents starting more work and rejects
late results, but cannot interrupt a running producer or validator. Producers
receive the remaining seconds and must enforce their own process timeouts.
"""
from dataclasses import dataclass, replace
import math
from time import monotonic
from typing import Callable, Iterable

from .errors import ValidationError
from .routing import ValidationReport, plan_digest
from .ses import RoutePlan, Track, Via


CALLBACK_LIMITATION = (
    "Synchronous callbacks cannot be interrupted by the portfolio deadline; "
    "producers and validators must enforce their own timeouts. Late results cannot win."
)


class PortfolioCancelled(ValidationError):
    """The search was cancelled; no prior candidate is returned for approval."""


@dataclass(frozen=True)
class PortfolioBudget:
    max_candidates: int
    max_seconds: float
    first_feasible: bool = False

    def __post_init__(self):
        if (type(self.max_candidates) is not int or self.max_candidates < 1
                or type(self.max_seconds) not in {int, float}
                or not math.isfinite(self.max_seconds) or self.max_seconds <= 0
                or type(self.first_feasible) is not bool):
            raise ValidationError("Portfolio budgets require positive finite time and an integer candidate limit.")


@dataclass(frozen=True)
class CandidateProducer:
    name: str
    produce: Callable[[float], RoutePlan]

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip() or not callable(self.produce):
            raise ValidationError("A candidate producer needs a name and a callable.")


@dataclass(frozen=True, order=True)
class CandidateMetrics:
    copper_length_mm: float
    via_count: int


@dataclass(frozen=True)
class CandidateResult:
    attempt: int
    producer: str
    plan: RoutePlan
    report: ValidationReport
    metrics: CandidateMetrics


@dataclass(frozen=True)
class CandidateDiagnostic:
    attempt: int
    producer: str
    status: str
    details: tuple[str, ...] = ()
    metrics: CandidateMetrics | None = None
    report: ValidationReport | None = None


@dataclass(frozen=True)
class PortfolioResult:
    winner: CandidateResult | None
    candidates: tuple[CandidateResult, ...]
    diagnostics: tuple[CandidateDiagnostic, ...]
    elapsed_seconds: float
    stop_reason: str
    callback_limitations: tuple[str, ...] = (CALLBACK_LIMITATION,)

    @property
    def selected_plan(self) -> RoutePlan | None:
        return self.winner.plan if self.winner else None

    @property
    def selected_report(self) -> ValidationReport | None:
        return self.winner.report if self.winner else None

    @property
    def selected_metrics(self) -> CandidateMetrics | None:
        return self.winner.metrics if self.winner else None


def _nm(value):
    if type(value) not in {int, float} or not math.isfinite(value) or abs(value) > 1e6:
        raise ValidationError("Candidate contains an invalid coordinate or dimension.")
    # The same quantization used by prepare_copper and the live item factory.
    return round(value * 1_000_000)


def _point(value):
    if type(value) is not tuple or len(value) != 2:
        raise ValidationError("Candidate points must be immutable coordinate pairs.")
    return tuple(_nm(number) for number in value)


def _name(value):
    if not isinstance(value, str) or not value:
        raise ValidationError("Candidate nets and layers must have explicit names.")
    return value


def candidate_geometry(plan: RoutePlan) -> tuple[tuple, CandidateMetrics]:
    """An order/direction-independent key and metrics on integer-nanometre copper.

    Same-net/layer/width collinear intervals are unioned, so reversed segments,
    polyline splitting and exact overlaps do not create new candidates or inflate
    length. Different nets, layers and widths remain distinct. This does not prove
    connectivity, clearance or the validity of any electrical constraint.
    """
    if (type(plan) is not RoutePlan or not isinstance(plan.base_design, str) or not plan.base_design
            or type(plan.tracks) is not tuple or type(plan.vias) is not tuple):
        raise ValidationError("Candidate must be an immutable RoutePlan.")
    groups = {}
    item_count = len(plan.vias)
    if item_count > 100_000:
        raise ValidationError("Candidate exceeds the 100,000 copper item limit.")
    for track in plan.tracks:
        if type(track) is not Track or type(track.points_mm) is not tuple or len(track.points_mm) < 2:
            raise ValidationError("Candidate contains an invalid track.")
        item_count += len(track.points_mm) - 1
        if item_count > 100_000:
            raise ValidationError("Candidate exceeds the 100,000 copper item limit.")
        width = _nm(track.width_mm)
        if not 0 < width <= 100_000_000:
            raise ValidationError("Candidate track width is outside supported bounds.")
        net, layer = _name(track.net), _name(track.layer)
        points = tuple(_point(point) for point in track.points_mm)
        for (ax, ay), (bx, by) in zip(points, points[1:]):
            dx, dy = bx - ax, by - ay
            divisor = math.gcd(dx, dy)
            if not divisor:
                raise ValidationError("Candidate has a zero-length segment after quantization.")
            ux, uy = dx // divisor, dy // divisor
            if ux < 0 or (ux == 0 and uy < 0):
                ux, uy = -ux, -uy
            line = (net, layer, width, ux, uy, ux * ay - uy * ax)
            a, b = ux * ax + uy * ay, ux * bx + uy * by
            groups.setdefault(line, []).append((min(a, b), max(a, b)))
    segments, lengths = [], []
    for line, intervals in sorted(groups.items()):
        merged = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        for start, end in merged:
            segments.append((*line, start, end))
            lengths.append((end - start) / math.hypot(line[3], line[4]) / 1_000_000)
    vias = set()
    for via in plan.vias:
        if type(via) is not Via:
            raise ValidationError("Candidate contains an invalid via.")
        spec = via.spec
        diameter, drill = _nm(spec.diameter_mm), _nm(spec.drill_mm)
        if (not 0 < drill < diameter <= 100_000_000 or type(spec.layers) is not tuple
                or len(spec.layers) != 2 or len(set(spec.layers)) != 2):
            raise ValidationError("Candidate contains an invalid quantized via.")
        layers = tuple(sorted(_name(layer) for layer in spec.layers))
        vias.add((_name(via.net), *_point(via.position_mm), diameter, drill, layers))
    if not segments and not vias:
        raise ValidationError("Candidate contains no copper.")
    # A changed base-design identity is not silently deduplicated against another design.
    key = (plan.base_design, tuple(segments), tuple(sorted(vias)))
    return key, CandidateMetrics(math.fsum(lengths), len(vias))


def _report_problems(report, digest, board_digest, required_ids):
    if type(report) is not ValidationReport:
        return ("Validator did not return a ValidationReport.",)
    problems = []
    if report.plan_digest != digest or report.board_digest != board_digest:
        problems.append("Validation evidence belongs to another plan or board.")
    if (type(report.enforced_constraint_ids) is not frozenset
            or report.enforced_constraint_ids != required_ids):
        problems.append("Validation evidence does not cover exactly the confirmed constraints.")
    if type(report.drc_violations) is not int or report.drc_violations != 0:
        problems.append("DRC is unknown or has blocking violations.")
    if type(report.unconnected_count) is not int or report.unconnected_count != 0:
        problems.append("Connectivity is unknown or incomplete.")
    if report.blocking_reasons:
        problems.append("Validator reported blocking reasons.")
    if ((report.routed_connections is None) != (report.total_connections is None)
            or (report.total_connections is not None
                and report.routed_connections != report.total_connections)):
        problems.append("Reported connection totals do not establish completion.")
    return tuple(problems)


def explore_candidates(
    producers: Iterable[CandidateProducer],
    validate: Callable[[RoutePlan], ValidationReport],
    *,
    budget: PortfolioBudget,
    expected_board_digest: str,
    required_constraint_ids: frozenset[str],
    assert_fresh: Callable[[], None],
    cancelled: Callable[[], bool],
    clock: Callable[[], float] = monotonic,
) -> PortfolioResult:
    """Explore sequentially and revalidate the winner to restore validator evidence.

    Freshness exceptions and cancellation propagate, discarding the search result.
    Callers must invalidate their session on these exceptions and must only adopt
    ``winner`` (never a diagnostic or an earlier candidate). Final validation is
    included in the time budget but not the producer-attempt count. No old report
    is restored into a validator and no DRC override or board-write API is used.
    """
    if (not isinstance(budget, PortfolioBudget) or not isinstance(expected_board_digest, str)
            or not expected_board_digest or type(required_constraint_ids) is not frozenset
            or any(not isinstance(value, str) or not value for value in required_constraint_ids)):
        raise ValidationError("Portfolio requires explicit board and constraint identities and valid budgets.")
    started = clock()
    deadline = started + budget.max_seconds
    diagnostics, candidates, seen = [], [], set()
    stop_reason = "candidate-limit"

    def check():
        if cancelled():
            raise PortfolioCancelled("Candidate search cancelled; previous results cannot be approved.")
        assert_fresh()  # Never caught as a candidate failure.
        if cancelled():
            raise PortfolioCancelled("Candidate search cancelled; previous results cannot be approved.")

    def remaining():
        return max(0.0, deadline - clock())

    def call(callback):
        check()
        if not remaining():
            return None, None, True
        value, error = None, None
        try:
            value = callback()
        except PortfolioCancelled:
            raise
        except Exception as exc:
            error = exc
        finally:
            check()  # Also detect stale inputs when the callback itself failed.
        return value, error, not remaining()

    def record(attempt, producer, status, *details, metrics=None, report=None):
        diagnostics.append(CandidateDiagnostic(attempt, producer, status, tuple(details), metrics, report))

    check()
    iterator = iter(producers)
    for attempt in range(1, budget.max_candidates + 1):
        check()
        if not remaining():
            stop_reason = "time-limit"
            break
        try:
            producer = next(iterator)
        except StopIteration:
            stop_reason = "producers-exhausted"
            break
        if not isinstance(producer, CandidateProducer):
            raise ValidationError("Portfolio entries must be named CandidateProducer instances.")
        plan, error, expired = call(lambda: producer.produce(remaining()))
        if expired:
            record(attempt, producer.name, "time-limit", "Producer exhausted the time budget.", CALLBACK_LIMITATION)
            stop_reason = "time-limit"
            break
        if error is not None:
            record(attempt, producer.name, "producer-failed", f"Producer raised {type(error).__name__}.")
            continue
        try:
            key, metrics = candidate_geometry(plan)
            digest = plan_digest(plan)
        except Exception as exc:
            record(attempt, producer.name, "invalid-plan", f"Candidate geometry rejected ({type(exc).__name__}).")
            continue
        check()
        if key in seen:
            record(attempt, producer.name, "duplicate", "Quantized copper duplicates an earlier candidate.", metrics=metrics)
            continue
        seen.add(key)
        report, error, expired = call(lambda: validate(plan))
        if expired:
            record(attempt, producer.name, "time-limit", "Validation exhausted the time budget.", CALLBACK_LIMITATION, metrics=metrics)
            stop_reason = "time-limit"
            break
        if error is not None:
            record(attempt, producer.name, "validation-failed", f"Validator raised {type(error).__name__}.", metrics=metrics)
            continue
        if plan_digest(plan) != digest:
            raise ValidationError("A candidate changed while validation was running; search invalidated.")
        problems = _report_problems(report, digest, expected_board_digest, required_constraint_ids)
        if problems:
            record(attempt, producer.name, "infeasible", *problems, metrics=metrics,
                   report=report if type(report) is ValidationReport else None)
            continue
        candidate = CandidateResult(attempt, producer.name, plan, report, metrics)
        candidates.append(candidate)
        record(attempt, producer.name, "feasible", metrics=metrics, report=report)
        if budget.first_feasible:
            # Speed mode still requires complete independent validation and the
            # final fresh validation below. It makes no optimality claim.
            stop_reason = "first-feasible"
            break

    winner = None
    check()
    if candidates:
        best = min(candidates, key=lambda candidate: (candidate.metrics, candidate.attempt))
        # Another candidate may have overwritten shared mutable validator evidence.
        # Restore it only by validating the selected plan again, under fresh inputs.
        digest = plan_digest(best.plan)
        report, error, expired = call(lambda: validate(best.plan))
        if expired:
            stop_reason = "time-limit"
            record(best.attempt, best.producer, "final-time-limit", "Winner could not be revalidated within the time budget.", CALLBACK_LIMITATION)
        elif error is not None:
            stop_reason = "final-validation-failed"
            record(best.attempt, best.producer, "final-validation-failed", f"Validator raised {type(error).__name__}.")
        else:
            if plan_digest(best.plan) != digest or digest != best.report.plan_digest:
                raise ValidationError("Selected candidate changed during search; search invalidated.")
            problems = _report_problems(report, digest, expected_board_digest, required_constraint_ids)
            if problems:
                stop_reason = "final-validation-failed"
                record(best.attempt, best.producer, "final-validation-failed", *problems,
                       report=report if type(report) is ValidationReport else None)
            else:
                winner = replace(best, report=report)
                record(best.attempt, best.producer, "selected", metrics=best.metrics, report=report)
    check()
    if winner is not None and not remaining():
        winner = None
        stop_reason = "time-limit"
        record(0, "portfolio", "final-time-limit", "Final freshness checks exhausted the time budget.")
    return PortfolioResult(winner, tuple(candidates), tuple(diagnostics), clock() - started, stop_reason)
