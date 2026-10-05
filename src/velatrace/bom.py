"""Deterministic BOM cost checks: value/package consolidation and JLCPCB Basic-part awareness.

Nothing here edits a design or touches the network. The optional parts list comes from
parts_db and is only ever read from the local cache; without it those checks are skipped.
Assumptions (fees, merge window, roles) are documented in docs/bom.md.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import TYPE_CHECKING, Iterable

from .findings import Finding, Severity
from .models import Component, DesignSnapshot

if TYPE_CHECKING:  # parts_db imports this module; keep the runtime dependency one-way.
    from .parts_db import Part, PartsDB

# JLCPCB Economic PCBA: Basic and Preferred Extended parts have no loading fee; every other
# (Extended) unique part costs a one-off feeder-loading fee per order. Checked 2026-09-30 at
# https://jlcpcb.com/help/article/pcb-assembly-faqs. Review this date when fees change.
JLC_EXTENDED_FEE_USD = Decimal("3.00")
JLC_FEE_CHECKED = "2026-09-30"
DEFAULT_BOARDS_PER_ORDER = 5  # JLCPCB's minimum PCB order; setup fees are per order, not per board.
# Pull-up/pull-down values within this ratio are interchangeable in typical digital use.
MERGE_RATIO = Decimal("1.2")

SI_PREFIX = {"p": Decimal("1e-12"), "n": Decimal("1e-9"), "u": Decimal("1e-6"),
             "m": Decimal("1e-3"), "": Decimal(1), "k": Decimal("1e3"), "M": Decimal("1e6"),
             "G": Decimal("1e9")}
_ALLOWED_PREFIX = {"resistor": {"", "m", "k", "M", "G"},
                   "capacitor": {"", "p", "n", "u", "m"},
                   "inductor": {"", "p", "n", "u", "m"}}
_UNIT = {"resistor": {"", "r", "ω", "ohm", "ohms"}, "capacitor": {"", "f"}, "inductor": {"", "h"}}
_DIELECTRICS = {"X5R", "X7R", "X7S", "X6S", "X8R", "X5S", "Y5V", "Z5U", "C0G"}
_DIELECTRIC_ALIASES = {"NP0": "C0G", "NPO": "C0G", "COG": "C0G"}
# A replacement may be the same or a more stable dielectric, never a worse one.
_DIELECTRIC_OK = {
    "C0G": {"C0G"},
    "X8R": {"X8R", "C0G"},
    "X7R": {"X7R", "X7S", "X8R", "C0G"},
    "X7S": {"X7S", "X7R", "X8R", "C0G"},
    "X6S": {"X6S", "X7R", "X7S", "X8R", "C0G"},
    "X5R": {"X5R", "X6S", "X7R", "X7S", "X8R", "C0G"},
    "X5S": {"X5S", "X5R", "X6S", "X7R", "X7S", "X8R", "C0G"},
}
_DNP = {"DNP", "DNF", "NC", "NF", "NOPOP", "NP", "DNI"}
_PACKAGES = ("01005", "0201", "0402", "0603", "0805", "1206", "1210", "1812", "2010", "2512")
_PACKAGE_RE = re.compile(r"(?<![0-9])(" + "|".join(_PACKAGES) + r")(?![0-9])")
_NOT_CHIP_RE = re.compile(r"(?i)elec|tantal|\bCP_|polar|THT|axial|radial|array|network|trimmer")
_REF_KIND = (("resistor", re.compile(r"R\d+[A-Za-z]?")),
             ("capacitor", re.compile(r"C\d+[A-Za-z]?")),
             ("inductor", re.compile(r"L\d+[A-Za-z]?")))
_GROUND_RE = re.compile(r"(?i)(?:[ADP]?GND\w*|GND|VSS\w*|0V|EARTH|CHASSIS)")
_POWER_RE = re.compile(r"(?i)(?:[+-]?\d+(?:\.\d+)?V\d*|V(?:CC|DD|BUS|BAT|IN|SYS|IO|CORE|MAIN|USB)"
                       r"\w*|V\+)")
# Signal-side parts that make a resistor a simple pull (anything else: divider, RC, LED, ...).
_PULL_PEER_RE = re.compile(r"(?:U|IC|J|P|CN|CONN?|SW|S|BTN|TP|MOD|MCU|A)\d+[A-Za-z]?")
# Pins/nets whose resistor value sets a timing, gain, current, threshold or analog strap.
_PRECISION_TOKENS = frozenset({
    "FB", "VFB", "ADJ", "RT", "CT", "RC", "ISET", "IREF", "IPROG", "PROG", "RSET", "REXT",
    "RBIAS", "BIAS", "SS", "TR", "COMP", "ILIM", "LIM", "OCP", "OSC", "SENSE", "ISENSE",
    "VSENSE", "ISNS", "CC", "ID", "TIMER", "DELAY", "UVLO", "OVP", "HYS", "HYST", "TRIP",
    "REF", "VREF", "ADC", "AIN", "XIN", "XOUT", "XTAL", "XI", "XO", "CFG", "CONFIG", "MODE",
    "SEL", "ADDR", "INP", "INN", "THERM", "NTC", "TEMP", "VSET", "SETI", "SETV",
    # PHY/transceiver/LED-driver bias and reference resistors (e.g. EPHY_RTX 6k04 1%).
    "RTX", "RTXT", "EXTRES", "RREF", "IBIAS", "VBIAS", "RTERM"})
# Net-name tokens that prove a digital pull. Values outside E24 (4.99k, 6.04k: E48/E96/E192)
# are usually chosen for precision, so they only count as pulls on a net named like this.
_PULL_SIGNAL_TOKENS = frozenset({
    "SDA", "SCL", "SMBDAT", "SMBCLK", "RST", "RESET", "EN", "ENABLE", "CE", "CS", "INT", "IRQ",
    "ALERT", "BOOT", "TACH", "WAKE", "WAKEUP", "PG", "PGOOD", "PWRGD", "POK", "BTN", "BUTTON",
    "KEY", "TX", "TXD", "RX", "RXD", "RTS", "CTS", "DTR", "MISO", "MOSI", "SCK", "SCLK",
    "OE", "WP", "HOLD", "FAULT", "FLT", "READY", "RDY", "DONE", "STAT", "CHG", "DET", "GPIO",
    "IO", "SHDN", "SLEEP", "STBY", "PULLUP", "PULLDOWN"})
_OPAMP_PIN_RE = re.compile(r"(?i)[+-]|IN\d*[+-]|[+-]IN\d*|V[+-]")
# Field names are compared after _field_key(): case, spaces and _-.#:/ punctuation ignored.
# Supplier fields (SPN1, Supplier Part Number, ...) only count when the value looks like C<digits>.
_LCSC_FIELDS = frozenset({
    "lcsc", "lcsc part", "lcsc part number", "lcsc part no", "lcsc pn", "lcsc no", "lcsc id",
    "jlc", "jlcpcb", "jlc part", "jlcpcb part", "jlc part number", "jlcpcb part number",
    "jlc part no", "jlcpcb part no", "jlc pn", "jlcpcb pn", "spn", "spn1", "spn2",
    "supplier part", "supplier part number", "supplier part no", "supplier pn",
    "supplier 1 part number", "supplier part number 1"})
_MPN_FIELDS = frozenset({
    "mpn", "mpn1", "manufacturer part", "manufacturer part number", "manufacturer part no",
    "manufacturer partno", "manufacturer pn", "mfr part", "mfr part number", "mfr part no",
    "mfr pn", "mfg part number", "mfg pn", "part number", "partno", "part no", "pn", "p n"})
_IGNORED_VALUE_WORDS = frozenset({"RES", "RESISTOR", "CAP", "CAPACITOR", "SMD", "MLCC", "CHIP",
                                  "CERAMIC"}) | frozenset(_PACKAGES)
_E24 = frozenset(Decimal(x) for x in (
    "1", "1.1", "1.2", "1.3", "1.5", "1.6", "1.8", "2", "2.2", "2.4", "2.7", "3", "3.3", "3.6",
    "3.9", "4.3", "4.7", "5.1", "5.6", "6.2", "6.8", "7.5", "8.2", "9.1"))
_E12 = frozenset(Decimal(x) for x in ("1", "1.2", "1.5", "1.8", "2.2", "2.7", "3.3", "3.9",
                                      "4.7", "5.6", "6.8", "8.2"))
_PULL_ROLES = frozenset({"pullup", "pulldown"})
_ECONOMIC = ("basic", "preferred")  # no JLCPCB loading fee; anything else is Extended
_MOVABLE_ROLES = _PULL_ROLES | {"decoupling"}


@dataclass(frozen=True)
class ParsedValue:
    value: Decimal
    tolerance: Decimal | None = None  # percent
    voltage: Decimal | None = None
    dielectric: str | None = None
    power: str | None = None
    other: frozenset[str] = frozenset()

    def compatible(self, other: "ParsedValue", *, ignore_voltage: bool = False) -> bool:
        """Same nominal part: no conflicting rating or unexplained extra text.

        A tolerance stated on only one side is a conflict ('10k 0.1%' is not a generic 10k).
        ignore_voltage is for members that share one MPN: the MPN fixes the real rating.
        """
        if (self.tolerance is None) != (other.tolerance is None):
            return False
        pairs = [(self.tolerance, other.tolerance), (self.dielectric, other.dielectric),
                 (self.power, other.power)]
        if not ignore_voltage:
            pairs.append((self.voltage, other.voltage))
        for mine, theirs in pairs:
            if mine is not None and theirs is not None and mine != theirs:
                return False
        return self.other == other.other


@dataclass(frozen=True)
class Passive:
    component: Component
    kind: str
    parsed: ParsedValue
    package: str

    @property
    def ref(self) -> str:
        return self.component.reference

    @property
    def line(self) -> tuple[str, Decimal, str]:
        return (self.kind, self.parsed.value, self.package)


@dataclass(frozen=True)
class _Role:
    role: str  # pullup | pulldown | decoupling | other
    rail: str = ""
    signal: str = ""


def _number(text: str) -> Decimal | None:
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _value_token(token: str, kind: str) -> Decimal | None:
    token = token.replace("µ", "u").replace("μ", "u")
    if kind == "resistor" and (leading := re.fullmatch(r"[Rr](\d+)", token)):
        return Decimal("0." + leading.group(1))  # R10 = 0.10 ohm
    rkm = re.fullmatch(r"(\d+)([pnumkKMGRrPNU])(\d+)([A-Za-zΩωΩ]*)", token)
    if rkm:
        whole, prefix, fraction, unit = rkm.groups()
        prefix = _canonical_prefix(prefix, kind)
        if prefix is None or unit.casefold() not in _UNIT[kind]:
            return None
        return Decimal(f"{whole}.{fraction}") * SI_PREFIX[prefix]
    plain = re.fullmatch(r"(\d+(?:\.\d*)?|\.\d+)([pnumkKMGPNU]?)([A-Za-zΩωΩ]*)", token)
    if not plain:
        return None
    number, prefix, unit = plain.groups()
    prefix = _canonical_prefix(prefix, kind)
    if prefix is None or unit.casefold() not in _UNIT[kind]:
        return None
    if kind != "resistor" and not prefix and not unit:
        return None  # a bare capacitor/inductor number has no unambiguous unit
    base = _number(number)
    return None if base is None else base * SI_PREFIX[prefix]


def _canonical_prefix(prefix: str, kind: str) -> str | None:
    if prefix in {"R", "r"}:
        return "" if kind == "resistor" else None
    if prefix in {"K"}:
        prefix = "k"
    elif prefix in {"P", "N", "U"}:
        prefix = prefix.lower()
    return prefix if prefix in _ALLOWED_PREFIX[kind] else None


def parse_value(text: str, kind: str) -> ParsedValue | None:
    """Parse passive value text such as 4k7, 4.7k, 4700, 10u, 100nF 50V X7R, 0R, 1k 1%,
    4,7uF (comma decimal), 10 kΩ, 2R2, '0402 10uF' (package/kind words are ignored)."""
    if kind not in _UNIT or not isinstance(text, str) or len(text) > 120:
        return None
    text = re.sub(r"(?<=\d),(?=\d)", ".", text.strip())  # European decimal comma
    tokens = [token for token in re.split(r"[\s,;_]+|(?<!\d)/|/(?!\d)", text)
              if token and token.upper() not in _IGNORED_VALUE_WORDS]
    if len(tokens) > 1 and re.fullmatch(r"\d+(?:\.\d+)?", tokens[0]) and re.fullmatch(
            r"(?i)[pnumkgrµμ]?(?:[ΩωΩ]|ohms?|f|h)?", tokens[1]):
        tokens[:2] = [tokens[0] + tokens[1]]  # '10 kΩ', '100 nF' (before the DNP 'NF' check)
    if not tokens or any(token.upper() in _DNP for token in tokens):
        return None
    value = _value_token(tokens[0], kind)
    if value is None or value < 0:
        return None
    tolerance = voltage = dielectric = power = None
    other = set()
    for token in tokens[1:]:
        upper = token.upper()
        if token in {"±", "+/-"}:
            continue
        if match := re.fullmatch(r"±?(\d+(?:\.\d+)?)%", token):
            tolerance = Decimal(match.group(1))
        elif match := re.fullmatch(r"(\d+(?:\.\d+)?)V", upper):
            voltage = Decimal(match.group(1))
        elif match := re.fullmatch(r"(\d+)V(\d+)", upper):
            voltage = Decimal(f"{match.group(1)}.{match.group(2)}")
        elif upper in _DIELECTRICS or upper in _DIELECTRIC_ALIASES:
            dielectric = _DIELECTRIC_ALIASES.get(upper, upper)
        elif re.fullmatch(r"(\d+(?:\.\d+)?|\d+/\d+)M?W", upper):
            power = upper
        else:
            other.add(upper)
    return ParsedValue(value, tolerance, voltage, dielectric, power, frozenset(other))


def format_value(value: Decimal, kind: str) -> str:
    unit = {"resistor": "Ω", "capacitor": "F", "inductor": "H"}.get(kind, "")
    if value == 0:
        return "0" + unit
    prefixes = ("G", "M", "k", "", "m") if kind == "resistor" else ("", "m", "u", "n", "p")
    for prefix in prefixes:
        if abs(value) >= SI_PREFIX[prefix]:
            break
    mantissa = (value / SI_PREFIX[prefix]).normalize()
    text = format(mantissa, "f")
    return text + prefix + unit


def package_of(footprint: str) -> str | None:
    """Imperial chip size from a footprint such as Resistor_SMD:R_0402_1005Metric."""
    if not isinstance(footprint, str) or _NOT_CHIP_RE.search(footprint):
        return None
    match = _PACKAGE_RE.search(footprint.rsplit(":", 1)[-1])
    return match.group(1) if match else None


def passive_kind(comp: Component) -> str | None:
    declared = comp.kind.strip().casefold()
    reference = comp.reference.strip()
    for kind, pattern in _REF_KIND:
        if declared == kind or pattern.fullmatch(reference):
            library = comp.footprint.split(":", 1)[0].casefold()
            for other, _ in _REF_KIND:
                if other != kind and library.startswith(other):
                    return None  # reference and footprint library disagree
            return kind
    return None


def _field_key(name: str) -> str:
    return re.sub(r"[\s_\-.#:/]+", " ", name.casefold()).strip()


def _fields(comp: Component, names: frozenset[str]) -> list[str]:
    return [value.strip() for name, value in comp.fields.items()
            if isinstance(name, str) and _field_key(name) in names
            and isinstance(value, str) and value.strip()]


def lcsc_code(comp: Component) -> str | None:
    return next((value.upper() for value in _fields(comp, _LCSC_FIELDS)
                 if re.fullmatch(r"C\d{1,9}", value.upper())), None)


def mpn_of(comp: Component) -> str | None:
    return next((value for value in _fields(comp, _MPN_FIELDS)
                 if value.upper() not in {"~", "-", "?", "N/A", "NA", "DNP"}), None)


def passives(snapshot: DesignSnapshot) -> list[Passive]:
    found = []
    for comp in snapshot.components:
        kind = passive_kind(comp)
        package = package_of(comp.footprint) if kind else None
        parsed = parse_value(comp.value, kind) if kind and package else None
        if parsed is not None:
            found.append(Passive(comp, kind, parsed, package))
    return found


def base_net(name: str) -> str:
    return name.rsplit("/", 1)[-1].strip()


def is_ground(net: str) -> bool:
    return bool(_GROUND_RE.fullmatch(base_net(net)))


def is_power(net: str) -> bool:
    return not is_ground(net) and bool(_POWER_RE.fullmatch(base_net(net)))


def rail_voltage(net: str) -> Decimal | None:
    name = base_net(net).upper().lstrip("+")
    if match := re.fullmatch(r"(\d+)V(\d+)", name):
        return Decimal(f"{match.group(1)}.{match.group(2)}")
    if match := re.fullmatch(r"(\d+(?:\.\d+)?)V", name):
        return Decimal(match.group(1))
    return Decimal(5) if name in {"VBUS", "VUSB"} else None


def _precision_name(name: str) -> bool:
    if _OPAMP_PIN_RE.fullmatch(name.strip()):
        return True
    for token in re.split(r"[^A-Za-z0-9]+", name.upper()):
        if token and (token in _PRECISION_TOKENS or token.rstrip("0123456789") in _PRECISION_TOKENS):
            return True
    return False


def _pull_signal_name(name: str) -> bool:
    for token in re.split(r"[^A-Za-z0-9]+", name.upper()):
        token = token.rstrip("0123456789")
        if token in _PULL_SIGNAL_TOKENS or (token[:1] == "N" and token[1:] in _PULL_SIGNAL_TOKENS):
            return True
    return False


def _mantissa(value: Decimal) -> Decimal:
    return value.scaleb(-value.adjusted()).normalize()


def classify_roles(snapshot: DesignSnapshot, found: Iterable[Passive]) -> dict[str, _Role]:
    """Conservative topology roles. Anything uncertain is 'other' and never value-merged."""
    graph = snapshot.connectivity()
    pin_names = {(comp.reference, pin.number): pin.name for comp in snapshot.components
                 for pin in comp.pins}
    roles = {}
    for passive in found:
        nets = [pin.net for pin in passive.component.pins]
        roles[passive.ref] = _Role("other")
        if len(passive.component.pins) != 2 or not all(nets) or nets[0] == nets[1]:
            continue
        if passive.kind == "capacitor":
            if any(map(is_ground, nets)) and any(map(is_power, nets)):
                rail = next(net for net in nets if is_power(net))
                roles[passive.ref] = _Role("decoupling", rail=rail)
            continue
        if passive.kind != "resistor":
            continue
        parsed = passive.parsed
        if not Decimal(1000) <= parsed.value <= Decimal(1_000_000) or (
                parsed.tolerance is not None and parsed.tolerance < 1):
            continue
        rails = [net for net in nets if is_power(net) or is_ground(net)]
        if len(rails) != 1:
            continue
        rail = rails[0]
        signal = nets[1] if nets[0] == rail else nets[0]
        peers = [node for node in graph.get(signal, ()) if node[0] != passive.ref]
        if (not peers or _precision_name(base_net(signal))
                or (_mantissa(parsed.value) not in _E24 and not _pull_signal_name(base_net(signal)))
                or any(not _PULL_PEER_RE.fullmatch(ref) for ref, _ in peers)
                or any(_precision_name(pin_names.get(node, "")) for node in peers
                       if pin_names.get(node))):
            continue
        roles[passive.ref] = _Role("pulldown" if is_ground(rail) else "pullup", rail, signal)
    return roles


def _ref_key(ref: str):
    match = re.fullmatch(r"([A-Za-z_#]*)(\d*)(.*)", ref)
    prefix, number, rest = match.groups() if match else (ref, "", "")
    return (prefix, int(number) if number else -1, rest)


def _refs(items: Iterable[Passive]) -> tuple[str, ...]:
    return tuple(sorted({item.ref for item in items}, key=_ref_key))


def _ref_list(refs: Iterable[str], limit: int = 8) -> str:
    refs = list(refs)
    return ", ".join(refs[:limit]) + (f" +{len(refs) - limit} more" if len(refs) > limit else "")


def _fee(lines: int, boards: int) -> Decimal:
    return (JLC_EXTENDED_FEE_USD * lines / boards).quantize(Decimal("0.01"))


def _fee_text(lines: int, boards: int) -> str:
    return (f"~${JLC_EXTENDED_FEE_USD * lines:.2f} JLCPCB Extended-part loading fee per order "
            f"(≈${_fee(lines, boards)}/board at {boards} boards; fee checked {JLC_FEE_CHECKED})")


def _line_text(kind: str, value: Decimal, package: str) -> str:
    return f"{format_value(value, kind)} {package}"


class _Context:
    def __init__(self, snapshot: DesignSnapshot, parts_db: "PartsDB | None", boards: int):
        self.snapshot = snapshot
        self.db = parts_db
        self.boards = boards
        self.passives = passives(snapshot)
        self.roles = classify_roles(snapshot, self.passives)
        self.by_line: dict[tuple, list[Passive]] = defaultdict(list)
        for passive in self.passives:
            self.by_line[passive.line].append(passive)
        self.package_use = Counter(passive.package for passive in self.passives)

    def role(self, passive: Passive) -> _Role:
        return self.roles.get(passive.ref, _Role("other"))

    def min_voltage(self, members: Iterable[Passive]) -> Decimal | None:
        needed = []
        for member in members:
            if member.parsed.voltage is not None:
                needed.append(member.parsed.voltage)
            role = self.role(member)
            if role.role == "decoupling" and rail_voltage(role.rail) is not None:
                needed.append(rail_voltage(role.rail))
        return max(needed) if needed else None

    def equivalents(self, kind: str, value: Decimal, package: str,
                    members: list[Passive]) -> list["Part"]:
        if self.db is None or kind not in {"resistor", "capacitor"}:
            return []
        tolerances = [m.parsed.tolerance for m in members if m.parsed.tolerance is not None]
        dielectrics = {m.parsed.dielectric for m in members if m.parsed.dielectric}
        if len(dielectrics) > 1:
            return []
        dielectric = next(iter(dielectrics), None)
        if kind == "capacitor" and dielectric is None and value <= Decimal("1e-9"):
            dielectric = "C0G"  # small unmarked caps are usually timing/RF: stay C0G
        return self.db.equivalents(kind, value, package, dielectric=dielectric,
                                   allowed_dielectrics=_DIELECTRIC_OK.get(dielectric or ""),
                                   min_voltage=self.min_voltage(members),
                                   max_tolerance=min(tolerances) if tolerances else None)

    def line_extended(self, line: tuple, members: list[Passive]) -> bool | None:
        """True when this BOM line would incur an Extended fee; None without parts data."""
        if self.db is None:
            return None
        codes = {lcsc_code(m.component) for m in members} - {None}
        if codes:
            return any(self.db.tier(code) not in _ECONOMIC for code in codes)
        return not self.equivalents(*line, members)


def _normalise_findings(ctx: _Context) -> list[Finding]:
    findings = []
    for line, members in ctx.by_line.items():
        texts = Counter(m.component.value.strip() for m in members)
        # One part number on every member, or none on any: a generic part and a specific
        # MPN/LCSC part (or two different MPNs) are different BOM lines on purpose.
        ids = {lcsc_code(m.component) or mpn_of(m.component) for m in members}
        same_part = len(ids) == 1 and None not in ids
        if len(texts) < 2 or len(ids) > 1 or any(
                not a.parsed.compatible(b.parsed, ignore_voltage=same_part)
                for a in members for b in members):
            continue
        kind, value, package = line
        volts = {m.parsed.voltage for m in members} - {None}
        # Never suggest a spelling that drops or lowers a stated voltage rating.
        canonical = max(texts, key=lambda text: (
            same_part or not volts or parse_value(text, kind).voltage == max(volts),
            texts[text], text == format_value(value, kind)))
        spelled = "; ".join(f"{_ref_list(_refs(m for m in members if m.component.value.strip() == t))}"
                            f" = '{t}'" for t in sorted(texts))
        same = "Same value, package and ratings"
        write = f"Write the value as '{canonical}'"
        if same_part and len(volts) > 1:
            same = (f"All carry part number {next(iter(ids))}, yet the text states "
                    f"{len(volts)} different voltage ratings; the part's datasheet rating is the real one")
            write = f"Write the value as '{format_value(value, kind)}' plus the part's real rating"
        findings.append(Finding(
            "bom.value_normalise", Severity.SAVING,
            f"{_line_text(kind, value, package)} {kind} value is written {len(texts)} ways",
            refs=_refs(members),
            evidence=(f"{spelled}. {same}, but BOM tools group by value "
                      f"text, so this becomes {len(texts)} BOM lines instead of 1."),
            fix=(f"{write} on all of them and give them one LCSC/MPN so "
                 f"the BOM has a single line ({len(texts) - 1} fewer to source and review).")))
    return findings


def _describe_role(ctx: _Context, member: Passive) -> str:
    role = ctx.role(member)
    direction = "up to" if role.role == "pullup" else "down to"
    return f"{member.ref} ({member.component.value.strip()}) pulls {role.signal} {direction} {role.rail}"


def _pick_target(ctx: _Context, candidates: list[tuple], counts: Counter):
    def economic(line):
        return ctx.db is not None and bool(ctx.equivalents(*line, ctx.by_line.get(line, [])))
    return max(candidates, key=lambda line: (counts[line], economic(line), _is_e12(line[1]),
                                            -line[1] if isinstance(line[1], Decimal) else 0))


def _is_e12(value: Decimal) -> bool:
    return value > 0 and _mantissa(value) in _E12


def _merge_cost(ctx: _Context, removed: list[tuple]) -> tuple[Decimal | None, str]:
    lines = len(removed)
    saved = f"{lines} fewer BOM line{'s' if lines != 1 else ''}/reel{'s' if lines != 1 else ''}"
    if ctx.db is None:
        return None, (f"{saved}. At JLCPCB each removed Extended line saves "
                      f"${JLC_EXTENDED_FEE_USD} per order; download the parts list to check.")
    extended = sum(1 for line in removed if ctx.line_extended(line, ctx.by_line[line]))
    if not extended:
        return None, f"{saved}; the removed line(s) are Basic/Preferred, so no JLCPCB fee changes."
    return -_fee(extended, ctx.boards), f"{saved}; saves {_fee_text(extended, ctx.boards)}."


def _value_merge_findings(ctx: _Context) -> list[Finding]:
    findings = []
    resistors_by_package: dict[str, set[Decimal]] = defaultdict(set)
    for passive in ctx.passives:
        if passive.kind == "resistor" and ctx.role(passive).role in _PULL_ROLES:
            resistors_by_package[passive.package].add(passive.parsed.value)
    for package, values in resistors_by_package.items():
        ordered = sorted(values)
        start = 0
        while start < len(ordered):
            end = start
            while end + 1 < len(ordered) and ordered[end + 1] <= ordered[start] * MERGE_RATIO:
                end += 1
            cluster = [("resistor", value, package) for value in ordered[start:end + 1]]
            start = end + 1
            members = [m for line in cluster for m in ctx.by_line[line]]
            if any(not a.parsed.compatible(b.parsed) for a in members for b in members):
                continue
            # Any other existing line of this package (a USB-C Rd 5.1k, say) can absorb the
            # pulls: its own parts never change, so its role does not matter.
            low, high = cluster[-1][1] / MERGE_RATIO, cluster[0][1] * MERGE_RATIO
            existing = [line for line in ctx.by_line if line[0] == "resistor"
                        and line[2] == package and line not in cluster and low <= line[1] <= high
                        and _mantissa(line[1]) in _E24
                        and all(a.parsed.compatible(b.parsed) for a in ctx.by_line[line]
                                for b in members)]
            candidates = cluster + existing
            if len(candidates) < 2:
                continue
            counts = Counter({line: len(ctx.by_line[line]) for line in candidates})
            target = _pick_target(ctx, candidates, counts)
            removed = [line for line in cluster if line != target and all(
                ctx.role(m).role in _PULL_ROLES for m in ctx.by_line[line])]
            if not removed:
                continue
            moved = [m for line in removed for m in ctx.by_line[line]]
            target_text = format_value(target[1], "resistor")
            cost, cost_note = _merge_cost(ctx, removed)
            if target in existing:
                cost_note = (f"{target_text} {package} is already on the BOM "
                             f"({_ref_list(_refs(ctx.by_line[target]), 4)}). {cost_note}")
            shown = sorted({line[1] for line in removed} | {target[1]})
            findings.append(Finding(
                "bom.value_merge", Severity.SAVING,
                f"Pull resistors {', '.join(format_value(v, 'resistor') for v in shown)}"
                f" ({package}) can share one value: {target_text}",
                refs=_refs(moved + ctx.by_line[target]),
                nets=tuple(sorted({ctx.role(m).signal for m in moved})),
                evidence=("; ".join(_describe_role(ctx, m) for m in moved[:6])
                          + (f"; +{len(moved) - 6} more" if len(moved) > 6 else "")
                          + f". Values are within {int((MERGE_RATIO - 1) * 100)}% of "
                          f"{target_text}, and each pulled net has only IC/connector/switch pins "
                          "besides the resistor (no RC timing, divider, feedback or LED current "
                          f"setting). {cost_note}"),
                fix=(f"Change {_ref_list(_refs(moved))} to {target_text} ({package}). Pull "
                     "strength changes by less than 20%; confirm bus rise time and input leakage "
                     "margins for your parts."),
                cost_delta=cost))
    return findings


def _package_merge_findings(ctx: _Context) -> list[Finding]:
    findings = []
    groups: dict[tuple, list[Passive]] = defaultdict(list)
    for passive in ctx.passives:
        groups[(passive.kind, passive.parsed.value)].append(passive)
    for (kind, value), members in groups.items():
        packages = Counter(m.package for m in members)
        if (len(packages) < 2 or kind == "inductor"
                or (kind == "capacitor" and value >= Decimal("1e-6"))
                or (kind == "resistor" and value < 1)
                or any(not a.parsed.compatible(b.parsed) for a in members for b in members)):
            continue
        lines = {(kind, value, package) for package in packages}
        counts = Counter({line: packages[line[2]] for line in lines})
        target = max(lines, key=lambda line: (counts[line], bool(
            ctx.db and ctx.equivalents(*line, members)), ctx.package_use[line[2]], line[2]))
        removed = sorted(line for line in lines if line != target and all(
            ctx.role(m).role in _MOVABLE_ROLES and m.parsed.power is None
            for m in ctx.by_line[line]))
        if not removed:
            continue
        moved = [m for line in removed for m in ctx.by_line[line]]
        volts = ctx.min_voltage(moved)
        cost, cost_note = _merge_cost(ctx, removed)
        breakdown = ", ".join(f"{p} ×{n}" for p, n in packages.most_common())
        roles = "; ".join(f"{m.ref} ({m.package}) {ctx.role(m).role} on "
                          f"{ctx.role(m).signal or ctx.role(m).rail}" for m in moved[:6])
        findings.append(Finding(
            "bom.package_merge", Severity.SAVING,
            f"{format_value(value, kind)} {kind}s use {len(packages)} packages ({breakdown}): "
            f"use {target[2]}",
            refs=_refs(members),
            nets=tuple(sorted({ctx.role(m).signal or ctx.role(m).rail for m in moved})),
            evidence=(f"{roles}. Same value and ratings in another package means an extra reel "
                      f"and BOM line. {cost_note}"),
            fix=(f"Change the footprint of {_ref_list(_refs(moved))} to {target[2]} and update the "
                 "layout" + ("" if kind != "capacitor" else f"; keep the voltage rating ≥ {volts}V"
                             if volts else "; confirm the voltage rating in the new package")
                 + "."),
            cost_delta=cost))
    return findings


def _part_text(part: "Part") -> str:
    ratings = " ".join(x for x in (f"{part.voltage.normalize():f}V" if part.voltage else "",
                                   part.dielectric or "",
                                   f"±{part.tolerance.normalize():f}%" if part.tolerance else "") if x)
    return f"{part.lcsc} ({part.tier.capitalize()}, {format_value(part.value, part.kind)} " \
           f"{part.package}{' ' + ratings if ratings else ''})"


def _jlc_findings(ctx: _Context) -> list[Finding]:
    db = ctx.db
    if db is None:
        return []
    findings: list[Finding] = []
    extended_lines: list[str] = []
    extended_refs: set[str] = set()
    fee_lines = 0
    by_code: dict[str, list[Component]] = defaultdict(list)
    passive_by_ref = {p.ref: p for p in ctx.passives}
    for comp in ctx.snapshot.components:
        code = lcsc_code(comp)
        if code:
            by_code[code].append(comp)
    for code, comps in sorted(by_code.items()):
        refs = tuple(sorted((c.reference for c in comps), key=_ref_key))
        part = db.part(code)
        members = [passive_by_ref[r] for r in refs if r in passive_by_ref]
        if part is not None:
            if part.value is not None and part.kind in {"resistor", "capacitor"}:
                wrong = [m for m in members if m.kind == part.kind and (
                    m.parsed.value != part.value or m.package != part.package)]
                if wrong:
                    findings.append(Finding(
                        "bom.lcsc_mismatch", Severity.WARNING,
                        f"{_ref_list(_refs(wrong))}: LCSC {code} is not the schematic value/package",
                        refs=_refs(wrong),
                        evidence=(f"Schematic says {wrong[0].component.value.strip()} "
                                  f"{wrong[0].package}; LCSC {_part_text(part)}."),
                        fix="Correct the LCSC field or the value before ordering; the assembler "
                            "places the part number, not the value text."))
            if part.tier in _ECONOMIC:
                continue
        fee_lines += 1
        extended_lines.append(f"{_ref_list(refs, 4)} ({code})")
        extended_refs.update(refs)
        if members and len(members) == len(refs) and len({m.line for m in members}) == 1:
            equivalents = ctx.equivalents(*members[0].line, members)
            if equivalents:
                best = equivalents[0]
                parts_delta, price_note = _price_change(part, best, len(refs), ctx.boards)
                findings.append(Finding(
                    "bom.jlc_basic_equivalent", Severity.SAVING,
                    f"{_ref_list(refs, 4)}: Extended {code} → {best.tier.capitalize()} {best.lcsc}"
                    f" available, saves ~${JLC_EXTENDED_FEE_USD} setup fee",
                    refs=refs,
                    evidence=((f"{code} is Extended in the JLCPCB catalogue" if part is not None else
                               f"{code} is not in the downloaded JLCPCB Basic/Preferred list")
                              + f" ({db.label}); {_part_text(best)} matches "
                              f"{members[0].component.value.strip()} {members[0].package}."
                              + price_note),
                    fix=f"Set the LCSC field of {_ref_list(refs)} to {best.lcsc}.",
                    cost_delta=-_fee(1, ctx.boards) + parts_delta))
    for line, members in sorted(ctx.by_line.items(), key=lambda item: _refs(item[1])):
        free = [m for m in members if not lcsc_code(m.component)]
        if not free or len(free) != len(members):
            continue
        mpn_parts = {db.find_mpn(mpn) for mpn in (mpn_of(m.component) for m in free) if mpn}
        if mpn_parts and None not in mpn_parts:
            continue  # manufacturer part numbers already resolve to Basic/Preferred parts
        kind, value, package = line
        equivalents = ctx.equivalents(kind, value, package, free)
        refs = _refs(free)
        if equivalents:
            best = equivalents[0]
            findings.append(Finding(
                "bom.jlc_assign", Severity.INFO,
                f"{_ref_list(refs, 4)} ({_line_text(kind, value, package)}): assign "
                f"{best.tier.capitalize()} part {best.lcsc}",
                refs=refs,
                evidence=(f"No LCSC field; {_part_text(best)} matches the value and package "
                          f"({db.label})."),
                fix=f"Set the LCSC field to {best.lcsc} so the assembler does not pick an "
                    "Extended part and nobody has to search for it."))
            continue
        alternative = _cheaper_alternative(ctx, line, free)
        fee_lines += 1
        extended_lines.append(f"{_ref_list(refs, 4)} ({_line_text(kind, value, package)})")
        extended_refs.update(refs)
        if alternative:
            title, evidence, fix = alternative
            findings.append(Finding("bom.jlc_basic_alternative", Severity.SAVING, title,
                                    refs=refs, evidence=evidence, fix=fix,
                                    cost_delta=-_fee(1, ctx.boards)))
    if fee_lines:
        findings.append(Finding(
            "bom.jlc_fee_summary", Severity.INFO,
            f"{fee_lines} Extended BOM line(s): ~${JLC_EXTENDED_FEE_USD * fee_lines:.2f} JLCPCB "
            "loading fee per order",
            refs=tuple(sorted(extended_refs, key=_ref_key)),
            evidence="; ".join(extended_lines[:12]) + (" …" if len(extended_lines) > 12 else "")
                     + f". {_fee_text(fee_lines, ctx.boards)}. Source: {db.label}, fetched "
                       f"{db.fetched_at or 'unknown'}. Passives without an LCSC field count when "
                       "no Basic/Preferred part matches; other parts count only with an LCSC field.",
            fix="Prefer Basic/Preferred parts where the design allows; other assemblers charge "
                "their own per-line setup fees."))
    return findings


def _price_change(old, new, per_board: int, boards: int) -> tuple[Decimal, str]:
    """Per-board parts-cost change from the two parts' own price breaks at the order quantity.
    Zero and no text when either price is unknown (the small list carries no prices)."""
    quantity = per_board * boards
    before = old.unit_price(quantity) if hasattr(old, "unit_price") else None
    after = new.unit_price(quantity) if hasattr(new, "unit_price") else None
    if before is None or after is None:
        return Decimal(0), ""
    return ((after - before) * per_board,
            f" Catalogue unit price at {quantity} pcs: ${before} → ${after}.")


def _cheaper_alternative(ctx: _Context, line: tuple, members: list[Passive]):
    kind, value, package = line
    roles = {ctx.role(m).role for m in members}
    if kind == "resistor" and roles <= _PULL_ROLES:
        options = []
        for candidate in ctx.db.values(kind, package):
            ratio = max(candidate, value) / min(candidate, value) if candidate and value else None
            if ratio is not None and ratio <= MERGE_RATIO:
                parts = ctx.equivalents(kind, candidate, package, members)
                if parts:
                    on_board = (kind, candidate, package) in ctx.by_line
                    options.append((parts[0].tier != "basic", not on_board, ratio, parts[0],
                                    candidate))
        if options:
            *_, part, candidate = min(options, key=lambda option: option[:3])
            return (f"{_ref_list(_refs(members), 4)}: no Basic {_line_text(kind, value, package)}"
                    f" — use {format_value(candidate, kind)} {part.lcsc} and save "
                    f"~${JLC_EXTENDED_FEE_USD} setup fee",
                    "; ".join(_describe_role(ctx, m) for m in members[:4])
                    + f". No Basic/Preferred part matches {format_value(value, kind)} {package}; "
                      f"{_part_text(part)} is within {int((MERGE_RATIO - 1) * 100)}%.",
                    f"Change {_ref_list(_refs(members))} to {format_value(candidate, kind)} and "
                    f"set LCSC {part.lcsc}; confirm pull strength margins.")
    if roles <= _MOVABLE_ROLES and kind in {"resistor", "capacitor"} and all(
            m.parsed.power is None for m in members):
        order = list(_PACKAGES)
        for other in sorted({"0402", "0603", "0805"} - {package},
                            key=lambda p: (-ctx.package_use[p], p)):
            smaller = order.index(other) < order.index(package)
            if kind == "capacitor" and value >= Decimal("1e-6") and smaller:
                continue  # DC-bias derating gets worse in smaller packages
            parts = ctx.equivalents(kind, value, other, members)
            if parts:
                return (f"{_ref_list(_refs(members), 4)}: no Basic {_line_text(kind, value, package)}"
                        f" — {other} Basic {parts[0].lcsc} saves ~${JLC_EXTENDED_FEE_USD} setup fee",
                        f"No Basic/Preferred part matches {format_value(value, kind)} {package}; "
                        f"{_part_text(parts[0])} has the same value and ratings.",
                        f"Change the footprint of {_ref_list(_refs(members))} to {other}, update "
                        f"the layout and set LCSC {parts[0].lcsc}.")
    return None


def suggested_parts(snapshot: DesignSnapshot, parts_db: "PartsDB | None") -> dict[str, "Part"]:
    """Reference -> best Basic/Preferred part for passives that carry no LCSC code yet, using
    the same rating rules as the bom.jlc_assign finding. Suggestions only."""
    ctx = _Context(snapshot, parts_db, DEFAULT_BOARDS_PER_ORDER)
    result = {}
    for line, members in ctx.by_line.items():
        if not any(lcsc_code(m.component) for m in members) and (
                found := ctx.equivalents(*line, members)):
            result.update({m.ref: found[0] for m in members})
    return result


def bom_findings(snapshot: DesignSnapshot, parts_db: "PartsDB | None" = None, *,
                 boards_per_order: int = DEFAULT_BOARDS_PER_ORDER) -> list[Finding]:
    """Money/time-saving BOM suggestions. Suggestions only; nothing is changed.

    Without parts_db, only offline consolidation checks run. cost_delta is per board: the
    per-order JLCPCB fee divided by boards_per_order.
    """
    if type(boards_per_order) is not int or not 1 <= boards_per_order <= 100_000:
        raise ValueError("boards_per_order must be an integer between 1 and 100000.")
    ctx = _Context(snapshot, parts_db, boards_per_order)
    return (_normalise_findings(ctx) + _value_merge_findings(ctx) + _package_merge_findings(ctx)
            + _jlc_findings(ctx))
