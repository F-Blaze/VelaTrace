"""Part-consistency checks: a Value, symbol or footprint that contradicts what the part itself says.

- part.mpn_value_mismatch:      resistor/capacitor Value disagrees with the value encoded in its MPN or description.
- part.footprint_kind_mismatch: capacitor on a resistor footprint, or the reverse.
- part.polarized_symbol:        tantalum/electrolytic capacitor drawn with a non-polar symbol.
- part.diode_type_mismatch:     Schottky symbol on a plain silicon diode value, or a Zener symbol on a rectifier.
- value.nonstandard:            fitted resistor value outside E24/E96/E192.

Each rule only decodes well-known part-number schemes and stays quiet whenever the data is not clear-cut.
"""
from __future__ import annotations

from decimal import Decimal
import math
import re

from . import audit_rules as ar
from . import bom
from .findings import Finding, Severity
from .models import Component, DesignSnapshot

_DESCRIPTION_FIELDS = frozenset({"description", "desc"})


def _eia(code: str) -> Decimal | None:
    """'1002' -> 100 x 10^2; 'R' is the decimal point ('49R9', '1R5', 'R100')."""
    if code.count("R") > 1:
        return None
    if "R" in code:
        return Decimal(code.replace("R", "."))
    return Decimal(code[:-1]) * Decimal(10) ** int(code[-1])


def _pf(code: str) -> Decimal | None:
    value = _eia(code)
    return None if value is None else value * Decimal("1e-12")


# (kind, anchored regex whose group 1 is the value code, decoder). Anything else is left alone.
_SCHEMES = tuple((kind, re.compile(pattern), decode) for kind, pattern, decode in (
    ("resistor", r"RC\d{4}[A-Z]R-(?:07|13|7W|7T)(\d*[RKM]\d*)L", ar.parse_resistance),            # Yageo RC
    ("resistor", r"RK73H\d[A-Z][A-Z]{2,3}([0-9R]{4})[DFG]", _eia),                                # KOA RK73 1%
    ("resistor", r"RK73B\d[A-Z][A-Z]{2,3}([0-9R]{3})[FGJ]", _eia),                                # KOA RK73 5%
    ("resistor", r"\d{4}W[A-Z0-9][BDF]([0-9R]{4})T[0-9A-Z]E", _eia),                              # Uniroyal 1%
    ("resistor", r"\d{4}W[A-Z0-9]J([0-9R]{3})T[0-9A-Z]E", _eia),                                  # Uniroyal 5%
    ("resistor", r"ERJ-?\d[A-Z]{2}F([0-9R]{4})[A-Z]", _eia),                                      # Panasonic ERJ 1%
    ("resistor", r"ERJ-?\d[A-Z]{2,3}J([0-9R]{3})[A-Z]", _eia),                                    # Panasonic ERJ 5%
    ("capacitor", r"CL(?:03|05|10|21|31|32|43)[ABCXY]([0-9R]{3})[BCDFGJKMZ][0-9A-Z]{5,6}", _pf),  # Samsung CL
    ("capacitor", r"G[RC]M[0-9A-Z]{3}[0-9A-Z]{2}[0-9][A-Z0-9]([0-9R]{3})[BCDFGJKMZ][A-Z0-9]{3}[A-Z]", _pf),  # Murata
    ("capacitor", r"CC\d{4}[BCDFGJKMZ][A-Z](?:X7R|X5R|X6S|X7S|NP0|NPO|C0G|Y5V)\d[A-Z]{2}([0-9R]{3})", _pf),  # Yageo CC
    ("capacitor", r"(?:MC)?\d{4}[NBXF]([0-9R]{3})[BCDFGJKMZ]\d{3}[A-Z]{2}", _pf),                 # Walsin / Multicomp
))


def _text_fields(comp: Component, names: frozenset[str]) -> list[tuple[str, str]]:
    return [(name, value.strip()) for name, value in comp.fields.items()
            if isinstance(name, str) and isinstance(value, str) and value.strip()
            and bom._field_key(name) in names]


def _decode_mpn(text: str, kind: str) -> Decimal | None:
    code = re.sub(r"\s+", "", text).upper()
    for scheme_kind, pattern, decode in _SCHEMES:
        match = pattern.fullmatch(code)
        if match and scheme_kind == kind:
            return decode(match.group(1))
    return None


_RES_UNIT = re.compile(r"(?i)\d+(?:[.,]\d+)?(?:[RKMΩ]\d*|OHMS?|K?Ω)")
_CAP_UNIT = re.compile(r"(?i)\d+(?:[.,]\d+)?(?:[PNUµμ]F?\d*|F)")


def _described(comp: Component, kind: str) -> list[tuple[str, str, Decimal]]:
    """Values a plain description spells out, e.g. 'Resistor 1K 1% 0402' (the kind word must match the part)."""
    found = []
    for name, text in _text_fields(comp, _DESCRIPTION_FIELDS):
        match = re.match(r"(?i)(res(?:istor)?|cap(?:acitor)?)[\s,:]+(\S+)", text)
        unit = _RES_UNIT if kind == "resistor" else _CAP_UNIT
        # Only a first token with its own unit is a value: "1 %" is a tolerance, "25V" a rating.
        if match and match.group(1)[:3].lower() == kind[:3] and unit.fullmatch(match.group(2)):
            parsed = bom.parse_value(match.group(2), kind)
            if parsed is not None:
                found.append((name, text, parsed.value))
    return found


def _same(a: Decimal, b: Decimal) -> bool:
    return a == b if b == 0 else abs(a - b) <= abs(b) * Decimal("0.01")


def check_mpn_value_mismatch(snapshot: DesignSnapshot) -> list[Finding]:
    """One finding per (value shown, value ordered, field): a batch of parts with one wrong part number
    is one mistake."""
    groups: dict[tuple[str, str, str], list[tuple[Component, str]]] = {}
    package: list[tuple[Component, str, str]] = []
    for comp in snapshot.components:
        kind = ar.part_kind(comp)
        if not comp.dnp and kind not in {"resistor", "capacitor"}:
            regulator = _regulator_package(comp)
            if regulator:
                package.append((comp, *regulator))
        if comp.dnp or kind not in {"resistor", "capacitor"}:
            continue
        shown = bom.parse_value(comp.value, kind)
        if shown is None:
            continue
        # "Part" is read here too: decoding only fires on an exact manufacturer scheme, so a loose field is safe.
        claims = [(name, text, _decode_mpn(text, kind)) for name, text in _text_fields(comp, _PART_FIELDS)]
        for name, text, wanted in [*claims, *_described(comp, kind)]:
            if wanted is not None and not _same(shown.value, wanted):
                have, want = bom.format_value(shown.value, kind), bom.format_value(wanted, kind)
                groups.setdefault((have, want, name), []).append((comp, text))
                break
        else:
            # Every decoded scheme is a surface-mount chip: on a through-hole footprint the wrong part gets bought.
            chip = next(((name, text) for name, text, wanted in claims if wanted is not None), None)
            if chip and _THT.search(comp.footprint):
                package.append((comp, *chip))
    findings = []
    for (have, want, name), rows in groups.items():
        refs = tuple(sorted((comp.reference for comp, _ in rows), key=_ref_order))
        shown = ", ".join(refs[:6]) + (", …" if len(refs) > 6 else "")
        comp, text = rows[0]
        findings.append(Finding(
            "part.mpn_value_mismatch", Severity.WARNING,
            f"{shown} {'is' if len(refs) == 1 else 'are'} labelled {have} but the {name} field says {want}", refs, (),
            f"{len(refs)} part{'s' if len(refs) > 1 else ''} with Value {have}; {comp.reference} field '{name}' = "
            f"'{text}' decodes to {want}.",
            f"Correct the Value to {want}, or fix the {name} field if {have} is what you want."))
    for (value, name, text), refs in _named_differently(snapshot).items():
        refs = tuple(sorted(refs, key=_ref_order))
        findings.append(Finding(
            "part.mpn_value_mismatch", Severity.WARNING,
            f"{', '.join(refs[:6])} Value {value} and {name} {text} name different parts", refs, (),
            f"Value '{value}' and field '{name}' = '{text}' share no part-number digits, so the schematic shows one part "
            "and the BOM orders another.",
            f"Make the Value and {name} name the same part (or note the substitute on purpose)."))
    for comp, name, text in package:
        chip = ar.part_kind(comp) in {"resistor", "capacitor"}
        thing = "a surface-mount chip part" if chip else "a surface-mount (DPAK/D2PAK) regulator"
        findings.append(Finding(
            "part.mpn_value_mismatch", Severity.WARNING,
            f"{comp.reference} part number {text} is {thing} but the footprint is through-hole", (comp.reference,), (),
            f"{comp.reference} field '{name}' = '{text}' orders {thing}; the footprint is "
            f"{comp.footprint.split(':', 1)[-1]}, so the bought part will not fit.",
            "Order the through-hole version of the part, or change the footprint to match the part number."))
    return findings


_PART_FIELDS = bom._MPN_FIELDS | {"part"}
# A part-number token: up to five letters then 3+ digits (B5819W, 2N7002, LM66100DCKR); package words are not.
_NUMBERED = re.compile(r"(\d?[A-Z]{1,5})(\d{3,})[A-Z0-9]*")
_PACKAGE_WORD = re.compile(r"SOT|SOD|SOIC|SOP|TSSOP|MSOP|QFN|DFN|TO|LED|SMA|SMB|SMC|DO|LQFP|TQFP|BGA|WLCSP|USB|HTSSOP|"
                           r"VQFN|WSON|SON|R|C|L")


def _part_digits(text: str) -> list[str]:
    found = []
    for token in re.split(r"[^A-Z0-9]+", text.upper()):
        match = _NUMBERED.fullmatch(token)
        if match and not _PACKAGE_WORD.fullmatch(match.group(1)):
            found.append(match.group(2))
    return found


def _named_differently(snapshot: DesignSnapshot) -> dict[tuple[str, str, str], list[str]]:
    """Diodes, transistors and ICs whose Value is a part number sharing no digit group with the MPN field
    (MMSD4148 vs CD1206-S01575). Equivalents from another maker keep their digits (B5819W vs 1N5819HW)."""
    groups: dict[tuple[str, str, str], list[str]] = {}
    for comp in snapshot.components:
        if comp.dnp or ar.part_kind(comp) not in {"diode", "transistor", "ic", "led"}:
            continue
        digits = _part_digits(comp.value)
        fields = [(name, text) for name, text in _text_fields(comp, _PART_FIELDS) if not re.fullmatch(r"C\d{3,}", text)]
        if not digits or not fields:
            continue
        flat = " ".join(re.sub(r"[^A-Z0-9]", "", text.upper()) for _, text in fields)
        if not any(d in flat for d in digits):
            name, text = fields[0]
            groups.setdefault((comp.value, name, text), []).append(comp.reference)
    return groups
_THT = re.compile(r"(?i)_THT|AXIAL|RADIAL|DIP-|TO-220|TO-92")
# 78xx/79xx linear regulators: ...DT (DPAK) and ...D2T (D2PAK) are surface-mount; ST, onsemi and TI share the suffixes.
_SMD_78XX = re.compile(r"(?:L|LM|MC|UA|KA)?7[89]M?\d\d[A-Z]*?(?:D2T|DT)(?:-?TR|G|R4G|RKG)?")


def _regulator_package(comp: Component) -> tuple[str, str] | None:
    for name, text in _text_fields(comp, _PART_FIELDS):
        code = re.sub(r"\s+", "", text).upper()
        if _SMD_78XX.fullmatch(code) and re.search(r"(?i)TO-220|TO-92|TO-3P", comp.footprint):
            return name, text
    return None


def _ref_order(ref: str) -> tuple[str, int]:
    match = re.match(r"([A-Za-z]+)(\d*)", ref)
    return (match.group(1), int(match.group(2) or 0)) if match else (ref, 0)


def _footprint_kind(footprint: str) -> str | None:
    library, _, name = footprint.rpartition(":")
    library, name = library.upper(), name.upper()
    if library.startswith("RESISTOR_"):
        return "resistor"
    if library.startswith("CAPACITOR_"):
        return "capacitor"
    if name.startswith("R_"):
        return "resistor"
    if name.startswith(("C_", "CP_")):
        return "capacitor"
    return None


def check_footprint_kind_mismatch(snapshot: DesignSnapshot) -> list[Finding]:
    wrong: dict[str, list[Component]] = {"capacitor": [], "resistor": []}
    for comp in snapshot.components:
        kind, footprint = ar.part_kind(comp), _footprint_kind(comp.footprint)
        if kind in wrong and footprint in wrong and footprint != kind:
            wrong[kind].append(comp)
    findings = []
    for kind, comps in wrong.items():
        if not comps:
            continue
        other = "resistor" if kind == "capacitor" else "capacitor"
        refs = tuple(sorted(comp.reference for comp in comps))
        findings.append(Finding(
            "part.footprint_kind_mismatch", Severity.INFO,
            f"{len(refs)} {kind}{'s' if len(refs) != 1 else ''} on {other} footprints: {', '.join(refs)}", refs, (),
            "; ".join(f"{comp.reference} ({comp.value}) uses {comp.footprint}" for comp in comps),
            f"Check whether the symbol or the footprint is wrong and assign a {kind} footprint."))
    return findings


_NONPOLAR_SYMBOL = re.compile(r"C(?:_Small)?(?:_US)?")
_POLAR_MPN = re.compile(r"TAJ[A-Z]\d{3}|TAP\d{3}|T49[15][A-Z]\d{3}|293D\d{3}|F93\d[A-Z0-9]{3}")
_POLAR_WORDS = re.compile(r"(?i)\b(?:tantalum|electrolytic|alumini?um)\b")
_NOT_POLAR = re.compile(r"(?i)non[\s-]?polar|bipolar|unpolari")


def check_polarized_symbol(snapshot: DesignSnapshot) -> list[Finding]:
    findings = []
    for comp in snapshot.components:
        lib_id = comp.fields.get("lib_id", "")
        if not lib_id or ar.part_kind(comp) != "capacitor" or not _NONPOLAR_SYMBOL.fullmatch(lib_id.rsplit(":", 1)[-1]):
            continue
        mpns = _text_fields(comp, bom._MPN_FIELDS)
        texts = [comp.value, *(text for _, text in _text_fields(comp, _DESCRIPTION_FIELDS)), *(t for _, t in mpns)]
        reason = next((f"its {name} field '{text}' is a tantalum part" for name, text in mpns
                       if _POLAR_MPN.match(re.sub(r"\s+", "", text).upper())), None)
        if reason is None:
            reason = next((f"'{text}' names a polarized capacitor" for text in texts
                           if _POLAR_WORDS.search(text) and not _NOT_POLAR.search(text)), None)
        if comp.footprint.rsplit(":", 1)[-1].upper().startswith("CP_"):
            # The footprint marks + (pad 1), so the board is marked; the risk is that pad 1 landed on ground,
            # since a non-polar symbol's pin 1 is arbitrary.
            pads = {pin.number: pin.net for pin in comp.pins}
            plus, minus = pads.get("1", ""), pads.get("2", "")
            if plus and minus and ar.is_ground(plus) and not ar.is_ground(minus):
                findings.append(Finding(
                    "part.polarized_symbol", Severity.WARNING,
                    f"{comp.reference}'s + pad is on {plus}: the polarized capacitor would be fitted backwards",
                    (comp.reference,), (plus, minus),
                    f"{comp.reference} uses non-polar symbol {lib_id} on polarized footprint {comp.footprint}; "
                    f"footprint pad 1 (+) is on {plus}, pad 2 (-) on {minus}.",
                    "Use a polarized symbol (for example Device:C_Polarized) with + on the supply side."))
            continue
        if reason:
            findings.append(Finding(
                "part.polarized_symbol", Severity.WARNING,
                f"{comp.reference} is a polarized capacitor drawn with a non-polar symbol", (comp.reference,), (),
                f"{comp.reference} uses symbol {lib_id} and footprint {comp.footprint} but {reason}, so the board "
                "has no polarity mark.",
                "Use a polarized symbol and footprint (for example Device:C_Polarized, CP_*) so + is marked."))
    # The reverse: a polarized symbol on a plain ceramic chip footprint. Ceramics have no polarity, but the symbol
    # invites a tantalum or electrolytic buyer, and nothing on the board marks +.
    ceramic = [comp.reference for comp in snapshot.components
               if ar.part_kind(comp) == "capacitor" and not comp.dnp
               and re.search(r"(?i)C_Polari[sz]ed|(?:^|:)CP(?:_|$)", comp.fields.get("lib_id", ""))
               and re.search(r"(?:^|:)C_(?:0201|0402|0603|0805|1206|1210|1812)_", comp.footprint)]
    if ceramic:
        refs = tuple(sorted(ceramic, key=_ref_order))
        findings.append(Finding(
            "part.polarized_symbol", Severity.INFO,
            f"{', '.join(refs[:6])}{', …' if len(refs) > 6 else ''} use a polarized symbol on a ceramic chip footprint",
            refs, (), f"{len(refs)} capacitor{'s' if len(refs) > 1 else ''} drawn with a polarized symbol sit on plain "
            "C_xxxx chip footprints: a ceramic has no polarity, and the symbol can lead to a tantalum or electrolytic "
            "being bought with no + mark on the board.",
            "Use the non-polar symbol (Device:C) for ceramics, or a CP_ footprint if a polarized part is intended."))
    return findings


_SUFFIX = r"(?:W|WS|WT|[-_/ ].*)?"
_SILICON = re.compile(r"(?:1N400[1-7]|1N4148|1N914|1N540[0-8]|BAV99|BAV70|US1[A-M]|S1[A-M]|M7)" + _SUFFIX)
_NOT_ZENER = re.compile(r"(?:1N400[1-7]|1N4148|1N914|1N58(?:1[7-9]|2[0-2])|SS[1-5]\d|B5819|B5817|B5822)" + _SUFFIX)


def check_diode_type_mismatch(snapshot: DesignSnapshot) -> list[Finding]:
    findings = []
    for comp in snapshot.components:
        lib_id = comp.fields.get("lib_id", "")
        symbol, value = lib_id.rsplit(":", 1)[-1].upper(), comp.value.strip().upper()
        if "SCHOTTKY" in symbol and _SILICON.fullmatch(value):
            drawn, actual = "Schottky", "a standard silicon diode"
        elif "ZENER" in symbol and _NOT_ZENER.fullmatch(value):
            drawn, actual = "Zener", "not a Zener"
        else:
            continue
        findings.append(Finding(
            "part.diode_type_mismatch", Severity.WARNING,
            f"{comp.reference} is drawn as a {drawn} diode but {comp.value} is {actual}", (comp.reference,), (),
            f"{comp.reference} uses symbol {lib_id} with value {comp.value}.",
            f"Use the symbol that matches {comp.value}, or change the value to the {drawn} part you meant."))
    return findings


_E96 = sorted({Decimal(f"{10 ** (k / 96):.2f}") for k in range(96)})
_E_SERIES = bom._E24 | set(_E96) | {Decimal(f"{10 ** (k / 192):.2f}") for k in range(192)}


def _nearest_e96(mantissa: Decimal) -> Decimal:
    return min([*_E96, Decimal(10)], key=lambda e: abs(math.log(float(e) / float(mantissa))))


def check_nonstandard_value(snapshot: DesignSnapshot) -> list[Finding]:
    groups: dict[Decimal, list[str]] = {}
    for comp in snapshot.components:
        if comp.dnp or ar.part_kind(comp) != "resistor":
            continue
        parsed = bom.parse_value(comp.value, "resistor")
        if parsed is None or not Decimal(1) <= parsed.value <= Decimal(10_000_000):
            continue
        mantissa = bom._mantissa(parsed.value)
        if len(mantissa.as_tuple().digits) < 2 or mantissa in _E_SERIES:
            continue
        orderable = (_decode_mpn(text, "resistor") for _, text in _text_fields(comp, bom._MPN_FIELDS))
        if any(want is not None and _same(parsed.value, want) for want in orderable) or bom.lcsc_code(comp):
            continue  # a decoded MPN or an LCSC code names a part that exists at this value
        groups.setdefault(parsed.value.normalize(), []).append(comp.reference)
    findings = []
    for value, refs in sorted(groups.items()):
        refs = tuple(sorted(refs))
        near = bom.format_value(_nearest_e96(bom._mantissa(value)) * Decimal(10) ** value.adjusted(), "resistor")
        text = bom.format_value(value, "resistor")
        findings.append(Finding(
            "value.nonstandard", Severity.INFO,
            f"{text} is not a standard E24/E96/E192 resistor value ({', '.join(refs)})", refs, (),
            f"{', '.join(refs)} use {text}; the nearest E96 value is {near}.",
            f"Use {near} (or another stocked value) unless {text} was chosen deliberately."))
    return findings
