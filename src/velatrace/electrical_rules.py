"""Pure, fail-closed compilation of electrical geometry into Specctra classes.

Widths are exact routing targets; clearance is a minimum. Allowed layers govern
tracks, not the copper barrel of a via. This does not prove impedance, return-path
continuity, or differential coupling. The caller must also run candidate DRC.
"""
from copy import deepcopy
from dataclasses import dataclass
import math
import re

from .dsn import dsn_scale
from .errors import ValidationError
from .ses import RoutePlan
from .sexpr import JoinedAtom, QuotedAtom, children, one, parse


def _name(value):
    return (isinstance(value, str) and bool(value) and '"' not in value
            and not any(ord(char) < 32 for char in value))


def _dimension(value):
    return type(value) in {int, float} and 0 < value <= 100 and math.isfinite(value)


@dataclass(frozen=True)
class NetRule:
    net: str
    allowed_layers: tuple[str, ...]
    widths_mm: tuple[tuple[str, float], ...] = ()
    clearance_mm: float | None = None

    def __post_init__(self):
        if (not _name(self.net) or type(self.allowed_layers) is not tuple
                or not self.allowed_layers or not all(_name(x) for x in self.allowed_layers)
                or len(set(self.allowed_layers)) != len(self.allowed_layers)):
            raise ValidationError("Electrical net/layer names must be unique and nonempty.")
        if (type(self.widths_mm) is not tuple
                or any(type(row) is not tuple or len(row) != 2 or not _name(row[0])
                       or not _dimension(row[1]) for row in self.widths_mm)):
            raise ValidationError("Electrical widths require immutable (layer, positive mm) pairs.")
        names = [row[0] for row in self.widths_mm]
        if len(names) != len(set(names)) or not set(names) <= set(self.allowed_layers):
            raise ValidationError("Electrical width layers must be unique and allowed.")
        if self.clearance_mm is not None and not _dimension(self.clearance_mm):
            raise ValidationError("Electrical clearance must be a positive finite dimension.")


@dataclass(frozen=True)
class ElectricalRules:
    net_rules: tuple[NetRule, ...] = ()
    version: int = 1

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise ValidationError("Unsupported electrical profile version.")
        if (type(self.net_rules) is not tuple
                or any(type(rule) is not NetRule for rule in self.net_rules)
                or len({rule.net for rule in self.net_rules}) != len(self.net_rules)):
            raise ValidationError("Electrical profiles require unique, immutable net rules.")


def _sections(node, allowed, offset=1):
    if any(not isinstance(row, list) or not row or row[0] not in allowed
           for row in node[offset:]):
        raise ValidationError(f"Unsupported electrical DSN content in {node[0]}.")


def _optional(node, name):
    matches = children(node, name)
    if len(matches) > 1:
        raise ValidationError(f"Duplicate electrical DSN {name} section.")
    return matches[0] if matches else None


def _numeric(value, *, zero=False):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValidationError("Unsupported electrical DSN numeric rule.") from None
    if not math.isfinite(result) or result < 0 or (not zero and result == 0):
        raise ValidationError("Electrical DSN dimensions must be positive and finite.")
    return result


def _rule(node, *, global_scope=False):
    """Validate the supported width/clearance grammar, retaining typed clearance."""
    if node is None:
        return {}
    result = {}
    for row in node[1:]:
        if (not isinstance(row, list) or len(row) < 2 or row[0] not in {"width", "clearance"}
                or not isinstance(row[1], str)):
            raise ValidationError("Unsupported electrical DSN rule shape.")
        _numeric(row[1], zero=row[0] == "clearance")
        key = (row[0],)
        if len(row) != 2:
            if (row[0] != "clearance" or len(row) != 3 or not isinstance(row[2], list)
                    or len(row[2]) != 2 or row[2][0] != "type" or not _name(row[2][1])):
                raise ValidationError("Unsupported electrical DSN rule shape.")
            kind = row[2][1]
            pair = kind.split("_")
            if not (len(pair) == 2 and set(pair) <= {"wire", "via", "pin", "smd", "area"}):
                if not (global_scope and kind == "smd_to_turn_gap"):
                    raise ValidationError("Unsupported electrical DSN clearance type.")
            key += (row[2][1],)
        if key in result:
            raise ValidationError("Duplicate electrical DSN rule.")
        result[key] = row
    return result


def _layer_rules(node, layers):
    result = {}
    for row in children(node, "layer_rule"):
        names = []
        for part in row[1:]:
            if isinstance(part, list):
                break
            names.append(part)
        if (not names or not all(_name(name) for name in names)
                or len(set(names)) != len(names) or not set(names) <= layers):
            raise ValidationError("Unknown or duplicate electrical DSN layer rule.")
        _sections(row, {"rule"}, len(names) + 1)
        rules = _rule(one(row, "rule"))
        for name in names:
            if name in result:
                raise ValidationError("Duplicate electrical DSN layer rule.")
            result[name] = rules
    return result


def _circuit(node, layers):
    if node is None:
        return None
    _sections(node, {"use_layer", "use_via", "length"})
    for name in ("use_layer", "use_via", "length"):
        row = _optional(node, name)
        if row is None:
            continue
        if name == "length":
            if len(row) != 3:
                raise ValidationError("Unsupported electrical DSN circuit length.")
            # Existing length metadata is preserved, not claimed as enforced.
            for value in row[1:]:
                try:
                    valid = isinstance(value, str) and math.isfinite(float(value)) and float(value) >= 0
                except ValueError:
                    valid = False
                if not valid:
                    raise ValidationError("Invalid electrical DSN circuit length.")
        elif (len(row) < 2 or not all(_name(x) for x in row[1:])
              or len(set(row[1:])) != len(row[1:])):
            raise ValidationError(f"Malformed electrical DSN {name}.")
        elif name == "use_layer" and not set(row[1:]) <= layers:
            raise ValidationError("Unknown electrical DSN circuit layer.")
    return _optional(node, "use_layer")


def _serialize(node):
    # Keep joined pin tokens and Specctra's literal quote declaration intact.
    if node == ["string_quote", '"']:
        return '(string_quote ")'
    def atom(value):
        if isinstance(value, list):
            return _serialize(value)
        if isinstance(value, JoinedAtom):
            return value.raw
        if value and not isinstance(value, QuotedAtom) and not re.search(r'[\s()"\\]', value):
            return value
        if '"' in value:
            raise ValidationError("DSN text contains a quote Specctra cannot represent.")
        return '"' + value + '"'
    return '(' + ' '.join(atom(value) for value in node) + ')'


def compile_electrical_dsn(text: str, rules: ElectricalRules) -> str:
    """Split targeted nets into classes; never emit ignored net-level layer rules.

    Existing use_layer is intersected. A requested width below an inherited width
    is a conflict. Stricter clearance values and all supported inherited circuit
    metadata survive. Cross-class clearance uses a conservative envelope because
    Freerouting 2.1 seeds it from the first class clearance; subsequent per-layer
    values alone do not protect pairs of different classes. Unsupported or
    ambiguous rule grammar is refused.
    """
    if type(rules) is not ElectricalRules:
        raise ValidationError("Expected an immutable electrical profile.")
    if not rules.net_rules:
        return text
    root = parse(text)
    if root[0] != "pcb" or len(root) < 3 or not isinstance(root[1], str):
        raise ValidationError("Expected a DSN PCB root.")
    scale = dsn_scale(root)
    structure, network = one(root, "structure"), one(root, "network")
    layer_rows = children(structure, "layer")
    layers, global_layers = set(), {}
    for row in layer_rows:
        if len(row) < 3 or not _name(row[1]) or row[1] in layers:
            raise ValidationError("Duplicate or malformed electrical DSN layer.")
        _sections(row, {"type", "property", "rule"}, 2)
        if one(row, "type") not in [["type", "signal"], ["type", "power"]]:
            raise ValidationError("Unsupported electrical DSN layer type.")
        layers.add(row[1])
        global_layers[row[1]] = _rule(_optional(row, "rule"), global_scope=True)
    if not layers:
        raise ValidationError("Electrical DSN has no copper layers.")
    defaults = _rule(one(structure, "rule"), global_scope=True)
    if children(structure, "layer_rule"):
        # 2.1 reads structure/layer/rule, not structure/layer_rule.
        raise ValidationError("Unsupported electrical DSN structure layer_rule; use layer/rule.")
    _sections(network, {"net", "class"})
    nets, classes, membership = {}, {}, {}
    for node in children(network, "net"):
        if len(node) < 3 or not _name(node[1]) or node[1] in nets:
            raise ValidationError("Duplicate or malformed electrical DSN net.")
        _sections(node, {"pins"}, 2)
        pins = one(node, "pins")[1:]
        if not all(isinstance(pin, str) for pin in pins) or len(set(pins)) != len(pins):
            raise ValidationError("Duplicate or unsupported electrical DSN pins.")
        nets[node[1]] = node
    for node in children(network, "class"):
        if len(node) < 2 or not _name(node[1]) or node[1] in classes:
            raise ValidationError("Duplicate or malformed electrical DSN class.")
        members = []
        for part in node[2:]:
            if isinstance(part, list):
                break
            members.append(part)
        _sections(node, {"rule", "layer_rule", "circuit"}, 2 + len(members))
        for net in members:
            if net not in nets or net in membership:
                raise ValidationError("Duplicate or unknown electrical DSN class net.")
            membership[net] = node
        _rule(_optional(node, "rule"))
        _layer_rules(node, layers)
        _circuit(_optional(node, "circuit"), layers)
        classes[node[1]] = node
    for index, policy in enumerate(rules.net_rules):
        if policy.net not in nets or not set(policy.allowed_layers) <= layers:
            raise ValidationError("Electrical profile contains an unknown net or layer.")
        original = membership.get(policy.net)
        inherited = deepcopy(original) if original else ["class", "", policy.net]
        options = [part for part in inherited[2:] if isinstance(part, list)]
        name = f"VelaTrace_Electrical_{index}"
        while name in classes:
            name += "_"
        classes[name] = inherited
        new_class = ["class", name, nets[policy.net][1], *options]
        circuit = _optional(new_class, "circuit")
        if circuit is None:
            circuit = ["circuit"]
            new_class.append(circuit)
        previous = _optional(circuit, "use_layer")
        allowed = tuple(layer for layer in policy.allowed_layers
                        if previous is None or layer in previous[1:])
        if not allowed or not set(dict(policy.widths_mm)) <= set(allowed):
            raise ValidationError("Electrical allowed layers conflict with inherited DSN rules.")
        if previous is not None:
            circuit.remove(previous)
        circuit.append(["use_layer", *allowed])
        class_rules = _rule(_optional(new_class, "rule"))
        layer_rules = _layer_rules(new_class, layers)
        clearance_values = [_numeric(row[1], zero=True)
                            for mapping in (defaults, *global_layers.values(), class_rules,
                                            *layer_rules.values())
                            for key, row in mapping.items() if key[0] == "clearance"]
        if policy.clearance_mm is not None:
            clearance_values.append(policy.clearance_mm / scale)
        if clearance_values:
            # First class clearance initializes clearances to OTHER classes in
            # pinned Freerouting. Splitting nets must not lose an old typed or
            # per-layer clearance between nets that formerly shared a class.
            # Keep the original scoped rules below; the stronger cross-class
            # envelope is intentional, not a claim of exact matrix equivalence.
            old_rule = _optional(new_class, "rule")
            if old_rule is not None:
                new_class.remove(old_rule)
            new_class.append(["rule", ["clearance", format(max(clearance_values), '.12g')],
                              *[deepcopy(row) for key, row in class_rules.items()
                                if key != ("clearance",)]])
        new_class[:] = [part for part in new_class
                        if not (isinstance(part, list) and part and part[0] == "layer_rule")]
        # Materialize effective per-layer rules, retaining typed clearance and
        # widths on untouched layers as well as the original class-wide rules.
        for layer in sorted(layers):
            effective = {**defaults, **global_layers.get(layer, {}), **class_rules,
                         **layer_rules.get(layer, {})}
            effective = deepcopy(effective)
            # This is a global pin-escape setting, not a class clearance pair.
            # Preserve its original scope; the class parser would ignore it.
            effective.pop(("clearance", "smd_to_turn_gap"), None)
            width = dict(policy.widths_mm).get(layer)
            if width is not None:
                inherited_width = effective.get(("width",))
                if inherited_width and width + 1e-9 < _numeric(inherited_width[1]) * scale:
                    raise ValidationError(f"Electrical width for {policy.net} on {layer} would weaken DSN width.")
                effective[("width",)] = ["width", format(width / scale, '.12g')]
            if policy.clearance_mm is not None:
                threshold = policy.clearance_mm / scale
                for key, row in effective.items():
                    if key[0] == "clearance":
                        row[1] = format(max(threshold, _numeric(row[1], zero=True)), '.12g')
                if ("clearance",) not in effective:
                    effective[("clearance",)] = ["clearance", format(threshold, '.12g')]
            if effective:
                new_class.append(["layer_rule", layer, ["rule", *effective.values()]])
        if original is not None:
            # A class may have the same name as its net: remove membership only.
            del original[original.index(policy.net, 2)]
        network.append(new_class)
    return _serialize(root)


def validate_plan_rules(plan: RoutePlan, rules: ElectricalRules,
                        layers: tuple[str, ...]) -> tuple[str, ...]:
    """Return geometry violations independently of the router's claimed success.

    Unprofiled nets retain their existing board rules. A via may cross layers
    excluded for tracks; only its known, distinct endpoints are checked here.
    Requested clearance remains an explicit unresolved issue: ordinary board
    DRC may use a weaker minimum. Callers must independently resolve this with
    board-specific geometry or a verified candidate rule overlay.
    """
    if type(rules) is not ElectricalRules or not isinstance(plan, RoutePlan):
        raise ValidationError("Expected a route plan and immutable electrical profile.")
    if (not layers or not all(_name(layer) for layer in layers)
            or len(set(layers)) != len(layers)):
        raise ValidationError("Electrical validation requires unique known copper layers.")
    known = set(layers)
    policies = {rule.net: rule for rule in rules.net_rules}
    if any(not set(rule.allowed_layers) <= known for rule in rules.net_rules):
        raise ValidationError("Electrical profile refers to an unknown copper layer.")
    issues = [f"{rule.net}: clearance {rule.clearance_mm:g} mm requires independent board-specific validation."
              for rule in rules.net_rules if rule.clearance_mm is not None]
    for track in plan.tracks:
        policy = policies.get(track.net)
        if track.layer not in known:
            issues.append(f"{track.net}: track uses unknown layer {track.layer}.")
        elif policy and track.layer not in policy.allowed_layers:
            issues.append(f"{track.net}: track uses forbidden layer {track.layer}.")
        if not _dimension(track.width_mm):
            issues.append(f"{track.net}: invalid track width on {track.layer}.")
        elif policy and (width := dict(policy.widths_mm).get(track.layer)) is not None:
            if not math.isclose(track.width_mm, width, rel_tol=0, abs_tol=1e-6):
                issues.append(f"{track.net}: track width on {track.layer} differs from {width:g} mm.")
    for via in plan.vias:
        span = via.spec.layers
        if len(span) != 2 or len(set(span)) != 2 or not set(span) <= known:
            issues.append(f"{via.net}: via has an unknown or invalid copper layer span.")
    return tuple(dict.fromkeys(issues))
