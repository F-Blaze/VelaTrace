"""Routing orchestration: state and user approval, never AI-generated trace paths.

Adapters must validate a candidate copy with actual KiCad DRC before approval.
No method here edits a board or invokes a shell. The writer is the sole mutation
boundary, and must re-check the board digest and backup before any mutation.
"""
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from enum import Enum
import hashlib
from time import perf_counter
from typing import Mapping, Protocol

from .constraints import Constraint, ConstraintStore
from .dsn import DsnInput
from .errors import CapabilityError, ValidationError
from .ses import RoutePlan, ViaSpec, parse_ses


class Mode(str, Enum):
    AUDIT = "Audit"
    ROUTING = "Routing"


class RoutingStage(str, Enum):
    SETUP = "setup"
    CONFIRMED = "confirmed"
    RUNNING = "running"
    VALIDATING = "validating"  # Routed; the preview may show while candidate DRC runs.
    PREVIEW = "preview"
    SHORTFALL = "shortfall"
    NEEDS_REASON = "needs-rejection-reason"
    REJECTED = "rejected"
    APPROVED = "approved"


@dataclass(frozen=True)
class ValidationReport:
    """Evidence computed by the actual candidate board validator, never the LLM.

    The digest must match repr(plan); DRC counts must include unconnected items.
    Each enforced constraint id attests numeric validation against actual board
    geometry. Unknown evidence is None and prevents approval.
    """
    plan_digest: str
    drc_violations: int | None
    unconnected_count: int | None
    routed_connections: int | None = None
    total_connections: int | None = None
    enforced_constraint_ids: frozenset[str] = frozenset()
    details: str = ""
    board_digest: str = ""
    preexisting_warnings: int = 0  # Present on the unrouted board too; reported, not blocking.
    blocking_reasons: tuple[str, ...] = ()

    def __post_init__(self):
        for value in (self.drc_violations, self.unconnected_count, self.routed_connections, self.total_connections,
                      self.preexisting_warnings):
            if value is not None and (type(value) is not int or value < 0):
                raise ValidationError("Routing validation counts must be non-negative integers or unknown.")
        if self.routed_connections is not None and self.total_connections is not None and self.routed_connections > self.total_connections:
            raise ValidationError("Routed connection count exceeds total connections.")

    @property
    def percent_routed(self) -> float | None:
        if self.routed_connections is None or self.total_connections in {None, 0}:
            return None
        return 100 * self.routed_connections / self.total_connections


def plan_digest(plan: RoutePlan) -> str:
    return hashlib.sha256(repr(plan).encode()).hexdigest()


class Router(Protocol):
    def check_startup(self) -> None: ...
    def route(self, dsn: DsnInput, constraints: tuple[Constraint, ...]) -> str:
        """Run external Freerouting, return bounded SES text; no board writes."""
        ...


class CandidateValidator(Protocol):
    """May also define prepare(dsn, constraints): route-independent warm-up (e.g. the
    unrouted-board baseline DRC) run concurrently with the router. It is a cache only;
    its errors are ignored because validate() re-derives and reports everything."""
    def supports(self, constraints: tuple[Constraint, ...]) -> bool:
        """True only when every numeric constraint can be enforced and checked."""
        ...
    def validate(self, dsn: DsnInput, plan: RoutePlan,
                 constraints: tuple[Constraint, ...]) -> ValidationReport:
        """Materialize temporary candidate and run real DRC. Never edit live board."""
        ...


class BoardWriter(Protocol):
    def apply(self, dsn: DsnInput, plan: RoutePlan, report: ValidationReport, *,
              drc_override: bool = False) -> None:
        """Revalidate, backup, then one undoable IPC commit. Fail without partial writes.
        drc_override lifts only the DRC-violation gate, and must be journaled."""
        ...


class RoutingSession:
    def __init__(self, constraints: ConstraintStore, router: Router, validator: CandidateValidator):
        self.constraints, self.router, self.validator = constraints, router, validator
        self.mode = Mode.AUDIT
        self.stage = RoutingStage.SETUP
        self.placed = False
        self.input: DsnInput | None = None
        self.plan: RoutePlan | None = None
        self.report: ValidationReport | None = None
        self.rejection_reason = ""
        self._needs_reason = False
        self._confirmation: tuple | None = None
        self.progress = lambda message: None
        self.timings: dict[str, float] = {}
        # Bumped whenever the current plan is replaced or dropped, so a background
        # check() started for an older plan can never be accepted.
        self.generation = 0
        self._warmup = None
        self.router.check_startup()

    def command(self, command: str) -> Mode:
        if command not in {"/autoroute", "/autoroute_exit"}:
            raise ValidationError("Unknown mode command.")
        if self.stage == RoutingStage.RUNNING:
            raise ValidationError("Wait for routing to stop before changing modes.")
        self.mode = Mode.ROUTING if command == "/autoroute" else Mode.AUDIT
        if self.mode == Mode.AUDIT:
            self._invalidate()
        return self.mode

    def _invalidate(self):
        self.generation += 1
        self._confirmation = None
        self.plan, self.report = None, None
        self.stage = RoutingStage.SETUP

    def cancel(self):
        """The Cancel button: drop a confirmed or previewed attempt; a check() still
        running for it can no longer be accepted. A running route() cancels itself."""
        if self.stage in {RoutingStage.CONFIRMED, RoutingStage.VALIDATING}:
            self._invalidate()

    def set_input(self, dsn: DsnInput, *, all_footprints_placed: bool):
        if not all_footprints_placed:
            raise ValidationError("Place every footprint and explicitly confirm placement before routing. No autoplacement.")
        dsn.assert_unchanged()
        self.input, self.placed = dsn, True
        self._invalidate()

    def add_constraint(self, constraint: Constraint):
        self.constraints.add(constraint)
        self._invalidate()

    def remove_constraint(self, identifier: str):
        self.constraints.remove(identifier)
        self._invalidate()

    def confirmation_text(self) -> str:
        lines = [item.description for item in self.constraints.items]
        return "Confirm routing with the existing copper stackup and:\n" + ("\n".join(lines) or "No additional constraints; board design rules still apply.")

    def confirm_constraints(self, fingerprint: str):
        if self.mode != Mode.ROUTING or not self.input or not self.placed:
            raise ValidationError("Enter routing mode, load a fresh DSN and confirm footprint placement first.")
        if self._needs_reason:
            raise ValidationError("What was wrong with the route? A reason is required before retrying.")
        if fingerprint != self.constraints.fingerprint:
            raise ValidationError("Constraint list changed; review all numeric constraints again.")
        if not self.validator.supports(self.constraints.items):
            raise CapabilityError("One or more constraints cannot be numerically enforced by this adapter; routing stopped. Remove or revise them explicitly.")
        self.input.assert_unchanged()
        self._confirmation = (fingerprint, self.input.digest, self.input.ticket.board_digest)
        self.stage = RoutingStage.CONFIRMED

    def _check_confirmation(self):
        if (self.mode != Mode.ROUTING or not self.input or self._confirmation !=
                (self.constraints.fingerprint, self.input.digest, self.input.ticket.board_digest)):
            raise ValidationError("Routing inputs or constraints changed; obtain a new confirmation.")
        self.input.assert_unchanged()

    def run(self, via_catalog: Mapping[str, ViaSpec] | None = None) -> ValidationReport:
        """Route and validate in one call. The UI calls route(), shows the preview,
        then runs check() off the UI thread and accept() back on it."""
        plan = self.route(via_catalog)
        generation = self.generation
        self.progress("Checking candidate DRC")
        try:
            report = self.check(plan)
        except Exception:
            self._invalidate()
            raise
        self.accept(plan, report, generation)
        return report

    def route(self, via_catalog: Mapping[str, ViaSpec] | None = None) -> RoutePlan:
        """Run the router and parse its result. Leaves stage VALIDATING: previewable,
        never approvable until check() evidence is accepted."""
        if self.stage != RoutingStage.CONFIRMED:
            raise ValidationError("Confirm concrete numeric constraints before each route attempt.")
        self._check_confirmation()
        self.stage = RoutingStage.RUNNING
        self.generation += 1
        self.plan, self.report = None, None
        self.timings = {}
        try:
            self.progress("Routing copper paths")
            started = perf_counter()
            prepare = getattr(self.validator, "prepare", None)
            pool = ThreadPoolExecutor(1)
            self._warmup = pool.submit(prepare, self.input, self.constraints.items) if prepare else None
            pool.shutdown(wait=False)  # check() waits for the warm-up; the preview does not.
            ses = self.router.route(self.input, self.constraints.items)
            self.timings["router"] = perf_counter() - started
            self._check_confirmation()
            plan = parse_ses(ses, expected_design=self.input.base_design or self.input.path.name, nets=set(self.input.nets),
                             layers=set(self.input.layers), via_catalog=via_catalog,
                             expected_placements=self.input.placements,
                             expected_placement_resolution_mm=self.input.placement_resolution_mm)
            self.plan = plan
            self.stage = RoutingStage.VALIDATING
            return plan
        except Exception:
            self._invalidate()
            raise

    def check(self, plan: RoutePlan) -> ValidationReport:
        """Candidate DRC for a routed plan. Safe off the UI thread: session state is
        only read here; the caller hands the result to accept() on its own thread."""
        if self._warmup is not None:
            wait([self._warmup])  # validate() never overlaps the baseline warm-up.
        started = perf_counter()
        report = self.validator.validate(self.input, plan, self.constraints.items)
        self.timings["validation"] = perf_counter() - started
        self._check_confirmation()
        if report.plan_digest != plan_digest(plan) or report.board_digest != self.input.ticket.board_digest:
            raise ValidationError("Candidate validator returned evidence for a different route or board.")
        if report.enforced_constraint_ids != frozenset(item.id for item in self.constraints.items):
            raise ValidationError("Not every confirmed constraint was validated.")
        return report

    def accept(self, plan: RoutePlan, report: ValidationReport | None, generation: int) -> bool:
        """Adopt check() evidence (None: the check failed). False, with nothing changed,
        when the plan was rerouted, rejected or otherwise invalidated meanwhile."""
        if generation != self.generation or self.stage != RoutingStage.VALIDATING or plan is not self.plan:
            return False
        if report is None:
            self._invalidate()
            return True
        self.report = report
        self.stage = (RoutingStage.SHORTFALL if report.unconnected_count is None or
                      report.unconnected_count > 0 else RoutingStage.PREVIEW)
        return True

    @property
    def summary(self) -> str:
        if self.plan is None or self.report is None:
            return "No routing result."
        percent = self.report.percent_routed
        completion = ("All connections verified" if self.report.unconnected_count == 0 else "Completion unknown") if percent is None else f"{percent:.1f}% routed"
        drc = "unknown" if self.report.drc_violations is None else str(self.report.drc_violations)
        text = f"{self.plan.trace_count} traces; {len(self.plan.vias)} vias; layers: {', '.join(self.plan.layers_used)}; DRC violations: {drc}; {completion}."
        if self.report.drc_violations:
            text += " Approval blocked: " + ("; ".join(self.report.blocking_reasons) or "resolve the reported DRC violations") + "."
        if self.report.preexisting_warnings:
            text += (f" {self.report.preexisting_warnings} pre-existing DRC warning(s) on the unrouted board"
                     " were not caused by this route and do not block approval; review them in KiCad.")
        if self.stage == RoutingStage.SHORTFALL:
            text += (" Routing stopped: completion verification is unavailable." if self.report.unconnected_count is None
                     else " Routing stopped: needs more layers or relaxed clearance.")
            text += " Stackup was not changed."
        return text

    def reject(self, reason: str):
        if not self._needs_reason and self.stage not in {RoutingStage.PREVIEW, RoutingStage.SHORTFALL,
                                                         RoutingStage.NEEDS_REASON, RoutingStage.VALIDATING}:
            raise ValidationError("No routing result is available to reject.")
        if not isinstance(reason, str) or not reason.strip():
            self.stage = RoutingStage.NEEDS_REASON
            self._needs_reason = True
            raise ValidationError("What was wrong with the route? Enter a reason; VelaTrace will not guess.")
        self.rejection_reason = reason.strip()
        self._needs_reason = False
        self.plan, self.report, self._confirmation = None, None, None
        self.generation += 1
        self.stage = RoutingStage.REJECTED
        # Constraints intentionally remain stacked. User supplies/reviews numeric adjustments.

    def approve(self, writer: BoardWriter, *, drc_override: bool = False):
        """drc_override is the user's explicit 'Approve anyway': it lifts only the
        known-DRC-violation gate. Unknown DRC, unconnected items and every board,
        preview and evidence check still refuse."""
        if self.stage != RoutingStage.PREVIEW or self.plan is None or self.report is None:
            raise ValidationError("Only a complete, validated preview may be approved.")
        self._check_confirmation()
        if self.report.unconnected_count != 0 or self.report.drc_violations is None:
            raise ValidationError("Approval requires known DRC results and zero unconnected items.")
        if self.report.drc_violations and not drc_override:
            raise ValidationError("Approval requires verified zero DRC violations; "
                                  "use ‘Approve anyway’ to override them explicitly.")
        writer.apply(self.input, self.plan, self.report, drc_override=drc_override)
        self.stage = RoutingStage.APPROVED
