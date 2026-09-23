"""Non-destructive candidate construction and independent official CLI validation."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from .dsn import DsnInput, dsn_scale, file_digest
from .errors import CapabilityError, ValidationError
from .routing import ValidationReport, plan_digest
from .ses import RoutePlan, ViaSpec
from .sexpr import QuotedAtom, children, one, parse


def read_board(path: Path):
    if path.stat().st_size > 32_000_000:
        raise ValidationError("Board exceeds the 32 MB safety limit.")
    text = path.read_text(encoding="utf-8")
    root = parse(text)
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
        rows.append(row)
    # KiCad may reorder top-level items when serializing. Their full contents remain compared.
    return tuple(sorted((visit(row) for row in rows), key=repr))


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
            if suffix == ".kicad_sch" and children(parse(path.read_text(encoding="utf-8")), "sheet"):
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
        if layers != set(dsn.layers) or not {"F.Cu", "B.Cu"} <= layers:
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


def prepare_copper(plan: RoutePlan, dsn: DsnInput) -> tuple[CopperItem, ...]:
    _, root = read_board(dsn.ticket.board_path)
    if any(children(root, kind) for kind in ("segment", "arc", "via")):
        raise CapabilityError("This routing adapter requires an unrouted board; existing copper is never replaced.")
    actual_layers = {row[1] for row in one(root, "layers")[1:] if isinstance(row, list) and len(row) > 2 and row[2] == "signal"}
    if actual_layers != set(dsn.layers):
        raise ValidationError("Saved board copper layers differ from DSN.")
    nets = {row[2] for row in children(root, "net") if len(row) == 3}
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
    root = parse(source)
    nets = {row[2]: int(row[1]) for row in children(root, "net")}
    lines = []
    for item in items:
        if item.kind == "segment":
            lines.append(f'(segment (start {item.start[0]:.6f} {item.start[1]:.6f}) (end {item.end[0]:.6f} {item.end[1]:.6f}) (width {item.width:.6f}) (layer "{item.layer}") (net {nets[item.net]}) (uuid "{item.id}"))')
        else:
            lines.append(f'(via (at {item.start[0]:.6f} {item.start[1]:.6f}) (size {item.width:.6f}) (drill {item.drill:.6f}) (layers "F.Cu" "B.Cu") (net {nets[item.net]}) (uuid "{item.id}"))')
    boundary = source.rfind(")")
    return source[:boundary] + "\n" + "\n".join(lines) + "\n" + source[boundary:]


class SafeCandidateValidator:
    def __init__(self, safety, cli):
        self.safety, self.cli = safety, cli
        self.evidence = None

    def supports(self, constraints):
        return all(item.kind in {"clearance", "trace-width"} and item.target == "all nets" for item in constraints)

    def validate(self, dsn, plan, constraints):
        self.evidence = None
        if not self.supports(constraints):
            raise CapabilityError("Only all-nets width and clearance constraints are validated.")
        dsn.assert_unchanged()
        self.safety.assert_matches(dsn)
        _, context = project_context(dsn.ticket.board_path)
        items = prepare_copper(plan, dsn)
        for constraint in constraints:
            if constraint.kind == "trace-width" and any(item.width + 1e-9 < constraint.minimum_mm for item in items if item.kind == "segment"):
                raise ValidationError("Router violated the confirmed minimum trace width.")
        source, _ = read_board(dsn.ticket.board_path)
        content = candidate_text(source, items)
        with tempfile.TemporaryDirectory(prefix="candidate-", dir=self.safety.directory) as folder:
            target = Path(folder) / dsn.ticket.board_path.name
            target.write_text(content, encoding="utf-8")
            for path, digest in context.items():
                if digest is not None:
                    shutil.copy2(path, Path(folder) / path.name)
            original = self.cli.drc(target)
            extra = original
            clearance = max((c.minimum_mm for c in constraints if c.kind == "clearance"), default=0)
            if clearance:
                rules = target.with_suffix(".kicad_dru")
                previous = rules.read_text(encoding="utf-8") if rules.exists() else "(version 1)\n"
                # Second pass supplements, never replaces proof under the original rules.
                rules.write_text(previous + f'\n(rule "VelaTrace confirmed clearance" (constraint clearance (min {clearance:.6f})))\n', encoding="utf-8")
                extra = self.cli.drc(target)
        dsn.assert_unchanged()
        if not context_matches(context):
            raise ValidationError("Project/rules changed during DRC; route again.")
        self.safety.assert_matches(dsn)
        report = ValidationReport(plan_digest(plan), max(original.violations + original.schematic_parity, extra.violations + extra.schematic_parity),
                                  max(original.unconnected, extra.unconnected),
                                  enforced_constraint_ids=frozenset(c.id for c in constraints),
                                  details="Official KiCad CLI candidate DRC; source board unchanged.", board_digest=dsn.ticket.board_digest)
        self.evidence = (dsn.digest, plan_digest(plan), report, items, context)
        return report
