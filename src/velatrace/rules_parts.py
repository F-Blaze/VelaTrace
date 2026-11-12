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


def _described(comp: Component, kind: str) -> list[tuple[str, str, Decimal]]:
    """Values a plain description spells out, e.g. 'Resistor 1K 1% 0402' (the kind word must match the part)."""
    found = []
    for name, text in _text_fields(comp, _DESCRIPTION_FIELDS):
        match = re.match(r"(?i)(res(?:istor)?|cap(?:acitor)?)[\s,:]+(\d.*)", text)
        if match and match.group(1)[:3].lower() == kind[:3]:
            parsed = bom.parse_value(match.group(2), kind)
            if parsed is not None:
                found.append((name, text, parsed.value))
    return found


def _same(a: Decimal, b: Decimal) -> bool:
    return a == b if b == 0 else abs(a - b) <= abs(b) * Decimal("0.01")


def check_mpn_value_mismatch(snapshot: DesignSnapshot) -> list[Finding]:
    findings = []
    for comp in snapshot.components:
        kind = ar.part_kind(comp)
        if comp.dnp or kind not in {"resistor", "capacitor"}:
            continue
        shown = bom.parse_value(comp.value, kind)
        if shown is None:
            continue
        claims = [(name, text, _decode_mpn(text, kind)) for name, text in _text_fields(comp, bom._MPN_FIELDS)]
        for name, text, wanted in [*claims, *_described(comp, kind)]:
            if wanted is not None and not _same(shown.value, wanted):
                have, want = bom.format_value(shown.value, kind), bom.format_value(wanted, kind)
                findings.append(Finding(
                    "part.mpn_value_mismatch", Severity.WARNING,
                    f"{comp.reference} is labelled {have} but its {name} field says {want}", (comp.reference,), (),
                    f"{comp.reference} Value '{comp.value}' ({have}); field '{name}' = '{text}' decodes to {want}.",
                    f"Correct the Value to {want}, or fix the {name} field if {have} is what you want."))
                break
    return findings


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
        if reason is None and comp.footprint.rsplit(":", 1)[-1].upper().startswith("CP_"):
            reason = f"its footprint {comp.footprint} is polarized"
        if reason:
            findings.append(Finding(
                "part.polarized_symbol", Severity.WARNING,
                f"{comp.reference} is a polarized capacitor drawn with a non-polar symbol", (comp.reference,), (),
                f"{comp.reference} uses symbol {lib_id} but {reason}, so the board has no polarity mark.",
                "Use a polarized symbol (for example Device:C_Polarized) so the + pin is marked and checked."))
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
        if any(want is not None and _same(parsed.value, want) for want in orderable):
            continue
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
