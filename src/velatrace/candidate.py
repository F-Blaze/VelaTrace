"""Non-destructive candidate construction and independent official CLI validation."""
from collections import Counter, namedtuple
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
import threading
import uuid

from .dsn import DsnInput, dsn_scale, file_digest
from .errors import CapabilityError, ValidationError
from .routing import ValidationReport, plan_digest
from .ses import RoutePlan, ViaSpec
from .sexpr import QuotedAtom, children, one, parse, render


def read_board(path: Path):
    if path.stat().st_size > 32_000_000:
        raise ValidationError("Board exceeds the 32 MB safety limit.")
    text = path.read_text(encoding="utf-8")
    root = parse(text, kicad=True)
    if root[0] != "kicad_pcb":
        raise ValidationError("Expected a saved KiCad PCB.")
    return text, root


def canonical(root, excluded_ids=frozenset()):
    """Conservative whole-document comparison, including unknown board geometry."""
    def visit(value):
        if isinstance(value, list):
            return tuple(visit(item) for item in value)
        if not isinstance(value, QuotedAtom):
            try:
                numeric = Decimal(value)
                if numeric.is_finite():
                    return ("number", numeric.normalize())
            except InvalidOperation:
                pass
        return str(value)
    rows = [root[0]]
    for row in root[1:]:
        if isinstance(row, list):
            if row[0] in {"generator", "generator_version"}:
                continue
            ids = children(row, "uuid")
            if ids and len(ids[0]) == 2 and ids[0][1] in excluded_ids:
                continue
            if row[0] == "footprint":
                # KiCad's in-memory board text stamps every footprint with file-format
                # metadata that the saved file carries only once at the top.
                row = [item for item in row if not (isinstance(item, list) and item
                       and item[0] in {"version", "generator", "generator_version"})]
        rows.append(row)
    # KiCad may reorder top-level items when serializing. Their full contents remain compared.
    return tuple(sorted((visit(row) for row in rows), key=repr))


def live_board_text(source: str, excluded_ids=frozenset()) -> str:
    """A candidate-only snapshot without this session's verified temporary items.

    Never writes the editor or saved board. Preserve quoted strings and unknown
    board fields when serializing the temporary validation file.
    """
    root = parse(source, kicad=True)
    if root[0] != "kicad_pcb":
        raise ValidationError("Expected a live KiCad PCB snapshot.")
    if not excluded_ids:
        return source
    def retained(row):
        ids = children(row, "uuid") if isinstance(row, list) else []
        return not (ids and len(ids[0]) == 2 and ids[0][1] in excluded_ids)
    return render([root[0], *(row for row in root[1:] if retained(row))])


def project_context(board_path: Path):
    project = board_path.with_suffix(".kicad_pro")
    if not project.is_file() or project.stat().st_size > 16_000_000:
        raise CapabilityError("Routing needs the saved matching .kicad_pro project to preserve design rules.")
    try:
        data = json.loads(project.read_text(encoding="utf-8"))
        settings = data["board"]["design_settings"]
        if not isinstance(settings, dict):
            raise ValueError()
        if settings.get("drc_exclusions"):
            raise CapabilityError("Remove DRC exclusions before routing; excluded checks cannot prove safety.")
        if any(value == "ignore" for value in settings.get("rule_severities", {}).values()):
            raise CapabilityError("Enable all DRC checks before routing; the project contains ignored checks.")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValidationError("Project design rules are missing or malformed.") from exc
    paths = [project]
    for suffix in (".kicad_dru", ".kicad_sch"):
        path = board_path.with_suffix(suffix)
        if path.exists():
            if not path.is_file() or path.stat().st_size > 32_000_000:
                raise ValidationError("Project context file is invalid or oversized.")
            if suffix == ".kicad_sch" and children(parse(path.read_text(encoding="utf-8"), kicad=True), "sheet"):
                raise CapabilityError("Hierarchical schematic context is not supported by candidate validation yet.")
        paths.append(path)
    return data, {path: file_digest(path) if path.exists() else None for path in paths}


def context_matches(context):
    return all((file_digest(path) if path.is_file() else None) == digest for path, digest in context.items())


def trusted_via_catalog(dsn: DsnInput) -> dict[str, ViaSpec]:
    """Accept only standard through-via names matched to actual saved project sizes."""
    data, _ = project_context(dsn.ticket.board_path)
    sizes = set()
    settings = data["board"]["design_settings"]
    for row in settings.get("via_dimensions", []):
        sizes.add((float(row["diameter"]), float(row["drill"])))
    for row in data.get("net_settings", {}).get("classes", []):
        if "via_diameter" in row and "via_drill" in row:
            sizes.add((float(row["via_diameter"]), float(row["via_drill"])))
    root = parse(dsn.path.read_text(encoding="utf-8"))
    scale = dsn_scale(root)
    result = {}
    for row in children(one(root, "library"), "padstack"):
        name = row[1]
        match = re.fullmatch(r"Via\[0-\d+\]_(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)_um", name)
        if not match:
            continue  # Ordinary footprint padstacks are not via definitions.
        diameter, drill = float(match[1]) / 1000, float(match[2]) / 1000
        if not any(abs(diameter-a) < 1e-6 and abs(drill-b) < 1e-6 for a, b in sizes):
            raise ValidationError("DSN via drill/diameter does not match saved project via settings.")
        layers = set()
        for shape in children(row, "shape"):
            circle = shape[1]
            if (len(circle) != 3 or circle[0] != "circle" or
                    abs(float(circle[2])*scale - diameter) > 1e-6):
                raise ValidationError("Only matching circular through-via padstacks are supported.")
            layers.add(circle[1])
        if layers != set(dsn.layers) or not {"F.Cu", "B.Cu"} <= dsn.board_layers:
            raise ValidationError("Via padstack must span every existing copper layer.")
        result[name] = ViaSpec(diameter, drill, ("F.Cu", "B.Cu"))
    return result


@dataclass(frozen=True)
class CopperItem:
    id: str
    kind: str
    net: str
    layer: str
    start: tuple[float, float]
    end: tuple[float, float] | None
    width: float
    drill: float = 0


def board_nets(root) -> dict[str, str]:
    """Net name -> the (net ...) body copper must use in this board's file format.

    KiCad 9 declares (net <code> "name") at top level; KiCad 10 dropped net codes and
    names nets inline, e.g. pads carry (net "VIN").
    """
    codes = {row[2]: row[1] for row in children(root, "net") if len(row) == 3}
    if codes:
        return codes
    return {net[1]: '"' + net[1].replace("\\", "\\\\").replace('"', '\\"') + '"'
            for fp in children(root, "footprint") for pad in children(fp, "pad")
            for net in children(pad, "net") if len(net) == 2 and net[1]}


def prepare_copper(plan: RoutePlan, dsn: DsnInput, *, source: str | None = None) -> tuple[CopperItem, ...]:
    root = parse(source, kicad=True) if source is not None else read_board(dsn.ticket.board_path)[1]
    if any(children(root, kind) for kind in ("segment", "arc", "via")):
        raise CapabilityError("This routing adapter requires an unrouted board; existing copper is never replaced.")
    # Inner planes are commonly typed power/mixed; they are still copper layers in the DSN.
    actual_layers = {row[1] for row in one(root, "layers")[1:] if isinstance(row, list) and len(row) > 2
                     and row[2] in {"signal", "power", "mixed", "jumper"}}
    if actual_layers != dsn.board_layers:
        raise ValidationError("Saved board copper layers differ from DSN.")
    nets = board_nets(root)
    catalog = set(trusted_via_catalog(dsn).values()) if plan.vias else set()
    output = []
    def q(value):
        if not math.isfinite(value) or abs(value) > 1e6:
            raise ValidationError("Invalid route coordinate.")
        return round(value * 1_000_000) / 1_000_000
    for track in plan.tracks:
        if track.net not in nets or track.layer not in actual_layers or len(track.points_mm) < 2:
            raise ValidationError("Route refers to unknown geometry, net or layer.")
        for start, end in zip(track.points_mm, track.points_mm[1:]):
            output.append(CopperItem(str(uuid.uuid4()), "segment", track.net, track.layer,
                                     (q(start[0]), q(-start[1])), (q(end[0]), q(-end[1])), q(track.width_mm)))
    for via in plan.vias:
        if via.net not in nets or via.spec not in catalog:
            raise ValidationError("Route via lacks verified project geometry.")
        output.append(CopperItem(str(uuid.uuid4()), "via", via.net, "F.Cu",
                                 (q(via.position_mm[0]), q(-via.position_mm[1])), None,
                                 q(via.spec.diameter_mm), q(via.spec.drill_mm)))
    if not output or len(output) > 100_000:
        raise ValidationError("Route is empty or exceeds 100,000 copper items.")
    for item in output:
        if (not 0 < item.width <= 100 or any(not math.isfinite(n) or abs(n) > 1e6 for n in (*item.start, *(item.end or ())))
                or (item.end is not None and item.start == item.end)):
            raise ValidationError("Invalid route geometry; nothing was written.")
    return tuple(output)


def candidate_text(source: str, items: tuple[CopperItem, ...]) -> str:
    root = parse(source, kicad=True)
    nets = board_nets(root)
    lines = []
    for item in items:
        if item.kind == "segment":
            lines.append(f'(segment (start {item.start[0]:.6f} {item.start[1]:.6f}) (end {item.end[0]:.6f} {item.end[1]:.6f}) (width {item.width:.6f}) (layer "{item.layer}") (net {nets[item.net]}) (uuid "{item.id}"))')
        else:
            lines.append(f'(via (at {item.start[0]:.6f} {item.start[1]:.6f}) (size {item.width:.6f}) (drill {item.drill:.6f}) (layers "F.Cu" "B.Cu") (net {nets[item.net]}) (uuid "{item.id}"))')
    boundary = source.rfind(")")
    return source[:boundary] + "\n" + "\n".join(lines) + "\n" + source[boundary:]


def _carried(baseline, candidate) -> Counter:
    """Candidate issues already on the unrouted board: same type, severity and items.
    An issue without items cannot be matched to anything, so it always counts as new."""
    both = Counter(candidate.issues) & Counter(baseline.issues)
    return Counter({issue: count for issue, count in both.items() if issue[2]})


def route_issues(baseline, candidate) -> tuple[int, int]:
    """(blocking, pre-existing warnings) for one DRC pass.

    A route may not add any DRC issue, and pre-existing errors still block. Warnings
    already on the unrouted board are reported only. Without issue identities every
    candidate issue blocks."""
    total = candidate.violations + candidate.schematic_parity
    if len(candidate.issues) != total or len(baseline.issues) != baseline.violations + baseline.schematic_parity:
        return total, 0
    warnings = sum(count for issue, count in _carried(baseline, candidate).items() if issue[1] == "warning")
    return total - warnings, warnings


def blocking_reasons(baseline, candidate) -> tuple[str, ...]:
    total = candidate.violations + candidate.schematic_parity
    if not total:
        return ()
    if len(candidate.issues) != total or len(baseline.issues) != baseline.violations + baseline.schematic_parity:
        return ("DRC issue identities unavailable; review the full KiCad DRC report",)
    carried = _carried(baseline, candidate)
    def summary(issues, note=""):
        counts = Counter()
        for (kind, severity, _), count in issues.items():
            counts[(kind, severity)] += count
        return tuple(f"{kind.replace('_', ' ')} ({severity}): {count}{note}" for (kind, severity), count in sorted(counts.items()))
    errors = Counter({issue: count for issue, count in carried.items() if issue[1] != "warning"})
    return summary(Counter(candidate.issues) - carried) + summary(errors, " already on the unrouted board")


def preexisting_errors(baseline, candidate) -> int:
    """Blocking issues of route_issues() that were already on the unrouted board."""
    total = candidate.violations + candidate.schematic_parity
    if len(candidate.issues) != total or len(baseline.issues) != baseline.violations + baseline.schematic_parity:
        return 0
    return sum(count for issue, count in _carried(baseline, candidate).items() if issue[1] != "warning")


def _parallel(function, values):
    """Run independent kicad-cli checks concurrently; results keep input order."""
    values = list(values)
    with ThreadPoolExecutor(max(1, len(values))) as pool:
        return list(pool.map(function, values))


# One inspect() result: candidate DRC of `items` for a plan, bound to the inputs checked.
Inspection = namedtuple("Inspection", "dsn_digest plan_digest constraints items context snapshot passes")


class SafeCandidateValidator:
    def __init__(self, safety, cli):
        self.safety, self.cli = safety, cli
        self.evidence = None
        # Baseline DRC of the unrouted board, keyed by the exact bytes KiCad checked.
        # ponytail: unbounded per-session cache, one small entry per board/rules revision.
        self._baselines = {}
        self._lock = threading.Lock()
        self._inspected = None  # Last inspect(): reused by validate() for the same plan.

    def supports(self, constraints):
        return all(item.kind in {"clearance", "trace-width"} and item.target == "all nets" for item in constraints)

    @staticmethod
    def _rule_sets(constraints):
        clearance = max((c.minimum_mm for c in constraints if c.kind == "clearance"), default=0)
        # The confirmed-clearance pass supplements, never replaces, proof under the original rules.
        return (0, clearance) if clearance else (0,)

    @staticmethod
    def _context_files(context):
        """Exact bytes of the digest-verified project context copied beside each candidate."""
        files = {}
        for path, digest in context.items():
            if digest is not None:
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ValidationError("Project/rules changed during DRC; route again.")
                files[path.name] = data
        return files

    @staticmethod
    def _with_rule(files, board_name, clearance):
        if not clearance:
            return files
        rules = Path(board_name).with_suffix(".kicad_dru").name
        extra = f'\n(rule "VelaTrace confirmed clearance" (constraint clearance (min {clearance:.6f})))\n'
        return {**files, rules: files.get(rules, b"(version 1)\n") + extra.encode("utf-8")}

    def _drc(self, board_name, board_text, files, baseline):
        files = {**files, board_name: board_text.encode("utf-8")}
        key = hashlib.sha256(repr(sorted((name, hashlib.sha256(data).hexdigest())
                                         for name, data in files.items())).encode()).hexdigest()
        if baseline:
            with self._lock:
                if key in self._baselines:
                    return self._baselines[key]
        with tempfile.TemporaryDirectory(prefix="candidate-", dir=self.safety.directory, ignore_cleanup_errors=True) as folder:
            for name, data in files.items():
                (Path(folder) / name).write_bytes(data)
            result = self.cli.drc(Path(folder) / board_name)
        if baseline:
            with self._lock:
                self._baselines[key] = result
        return result

    def prepare(self, dsn, constraints):
        """Warm the unrouted-board baseline DRC while the router runs.

        Only a cache warm-up: validate() re-reads the live board and project and
        reuses a result solely for byte-identical board, project and rules files.
        """
        if not self.supports(constraints):
            return
        source = self.safety.assert_matches(dsn)
        _, context = project_context(dsn.ticket.board_path)
        files, name = self._context_files(context), dsn.ticket.board_path.name
        _parallel(lambda clearance: self._drc(name, source, self._with_rule(files, name, clearance), True),
                  self._rule_sets(constraints))

    def inspect(self, dsn, plan, constraints, items=None):
        """Candidate DRC without approval evidence: an Inspection whose passes are
        (baseline, candidate) DRC results per rule set. The public hook for route repair.

        `items` defaults to the plan's copper. A repair passes a subset of an earlier
        call's items for the plan it derived from them: ids are kept, so DRC issues
        compare across calls, and the subset must be exactly that plan's copper.
        validate() reuses the latest result (or one handed back through reuse()) for
        the same plan, after re-checking that board, project and DSN are unchanged."""
        self.evidence = self._inspected = None
        if not self.supports(constraints):
            raise CapabilityError("Only all-nets width and clearance constraints are validated.")
        dsn.assert_unchanged()
        source = self.safety.assert_matches(dsn)
        snapshot = canonical(parse(source, kicad=True))
        _, context = project_context(dsn.ticket.board_path)
        expected = prepare_copper(plan, dsn, source=source)
        def shape(item):
            ends = (item.start, item.end) if item.end is None else tuple(sorted((item.start, item.end)))
            return item.kind, item.net, item.layer, ends, item.width, item.drill
        if items is None:
            items = expected
        elif Counter(map(shape, items)) != Counter(map(shape, expected)):
            raise ValidationError("Candidate copper differs from the route plan.")
        for constraint in constraints:
            if constraint.kind == "trace-width" and any(item.width + 1e-9 < constraint.minimum_mm for item in items if item.kind == "segment"):
                raise ValidationError("Router violated the confirmed minimum trace width.")
        content = candidate_text(source, items)
        files, name = self._context_files(context), dsn.ticket.board_path.name
        # (unrouted baseline, candidate) under identical rules, per rule set; independent, so concurrent.
        jobs = []
        for clearance in self._rule_sets(constraints):
            rules = self._with_rule(files, name, clearance)
            jobs += [(source, rules, True), (content, rules, False)]
        results = _parallel(lambda job: self._drc(name, *job), jobs)
        passes = list(zip(results[::2], results[1::2]))
        dsn.assert_unchanged()
        if not context_matches(context):
            raise ValidationError("Project/rules changed during DRC; route again.")
        self.safety.assert_matches(dsn, expected_board=snapshot)
        self._inspected = Inspection(dsn.digest, plan_digest(plan), tuple(constraints), tuple(items), context,
                                     snapshot, passes)
        return self._inspected

    def reuse(self, inspection: Inspection):
        """Make an earlier inspect() result the one validate() may reuse."""
        self._inspected = inspection

    def validate(self, dsn, plan, constraints):
        self.evidence = None
        cached = self._inspected
        if cached is not None and cached[:3] == (dsn.digest, plan_digest(plan), tuple(constraints)):
            # Same plan, constraints and DSN: the DRC results stand if nothing else moved.
            items, context, snapshot, passes = cached[3:]
            dsn.assert_unchanged()
            if not context_matches(context):
                raise ValidationError("Project/rules changed during DRC; route again.")
            self.safety.assert_matches(dsn, expected_board=snapshot)
        else:
            items, context, snapshot, passes = self.inspect(dsn, plan, constraints)[3:]
        self._inspected = None
        judged = [route_issues(baseline, candidate) for baseline, candidate in passes]
        unconnected = max(candidate.unconnected for _, candidate in passes)
        # Connections open on the unrouted board (pours filled) are the routing job.
        total = max(max(baseline.unconnected for baseline, _ in passes), unconnected)
        report = ValidationReport(plan_digest(plan), max(blocking for blocking, _ in judged), unconnected,
                                  routed_connections=total - unconnected, total_connections=total,
                                  enforced_constraint_ids=frozenset(c.id for c in constraints),
                                  details="Official KiCad CLI DRC of the candidate against the unrouted board; source board unchanged.",
                                  board_digest=dsn.ticket.board_digest,
                                  preexisting_warnings=max(carried for _, carried in judged),
                                  preexisting_errors=min(preexisting_errors(baseline, candidate)
                                                         for baseline, candidate in passes),
                                  blocking_reasons=tuple(dict.fromkeys(reason for baseline, candidate in passes
                                                                      for reason in blocking_reasons(baseline, candidate))))
        self.evidence = (dsn.digest, plan_digest(plan), report, items, context, snapshot)
        return report
