"""Route board's default adapter: Vela-routing behind the SES router contract."""
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")

from velatrace import candidate  # noqa: E402
from velatrace.constraints import Constraint, Scope  # noqa: E402
from velatrace.errors import CapabilityError, RoutingCancelled  # noqa: E402
from velatrace.ses import ViaSpec, parse_ses  # noqa: E402
from velatrace.vela_router import VelaRouter, session_text  # noqa: E402

VIA = "Via[0-1]_600:300_um"


def _pad(ref, x, y, net):
    return (f'(footprint "T" (layer "F.Cu") (at {x} {y}) (property "Reference" "{ref}") '
            f'(pad "1" smd rect (at 0 0) (size 1.2 1.2) (layers "F.Cu") (net "{net}")))')


def _dsn(tmp_path, aliases=None):
    """The exported board copy and the DsnInput fields the adapter reads."""
    body = _pad("A1", 3, 6, "A") + _pad("A2", 17, 6, "A") + _pad("B1", 10, 2, "B") + _pad("B2", 10, 10, "B")
    (tmp_path / "board.kicad_pcb").write_text(
        '(kicad_pcb (version 20260206) (generator "test") (general (thickness 1.6)) '
        '(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user)) '
        f'(gr_rect (start 0 0) (end 20 12) (layer "Edge.Cuts")) {body})', encoding="utf-8")
    return SimpleNamespace(path=tmp_path / "board.dsn", base_design="C:\\work\\board.dsn",
                           layer_aliases=aliases or {}, ticket=SimpleNamespace(board_path=Path("unused")))


def _parse(text, layers=frozenset({"F.Cu", "B.Cu"}), aliases=None):
    catalog = {VIA: ViaSpec(.6, .3, ("F.Cu", "B.Cu"))}
    return parse_ses(text, expected_design="C:\\work\\board.dsn", nets={"A", "B"}, layers=set(layers),
                     via_catalog=catalog, layer_aliases=aliases), catalog


def _width(minimum):
    return Constraint("w", Scope.SESSION, "trace-width", "all nets", minimum)


def test_route_returns_ses_that_the_existing_parser_accepts(tmp_path, monkeypatch):
    monkeypatch.setattr(candidate, "trusted_via_catalog", lambda dsn: {VIA: ViaSpec(.6, .3, ("F.Cu", "B.Cu"))})
    router, messages = VelaRouter(), []
    router.progress = messages.append
    plan, _ = _parse(router.route(_dsn(tmp_path), ()))
    assert router.last_report["unrouted"] == 0 and any(m.startswith("Routing · ") for m in messages)
    assert {t.net for t in plan.tracks} == {"A", "B"} and len(plan.vias) == 2
    # Specctra Y-up: the board's pads lie at KiCad y 2..10, so plan y is negative.
    assert all(-12 <= y <= 0 for t in plan.tracks for _, y in t.points_mm)
    assert all(t.width_mm == pytest.approx(.2) for t in plan.tracks)


def test_confirmed_width_raises_every_net_class(tmp_path, monkeypatch):
    monkeypatch.setattr(candidate, "trusted_via_catalog", lambda dsn: {VIA: ViaSpec(.6, .3, ("F.Cu", "B.Cu"))})
    plan, _ = _parse(VelaRouter().route(_dsn(tmp_path), (_width(.3),)))
    assert plan.tracks and all(t.width_mm >= .3 - 1e-9 for t in plan.tracks)


def test_unsupported_constraint_and_cancel_are_refused_before_routing(tmp_path):
    router = VelaRouter()
    with pytest.raises(CapabilityError):
        router.route(_dsn(tmp_path), (Constraint("h", Scope.SESSION, "header-clearance", "J1", 1),))
    router.cancel.set()
    with pytest.raises(RoutingCancelled):
        router.route(_dsn(tmp_path), ())


def test_session_text_uses_dsn_names_and_refuses_what_it_cannot_write(tmp_path):
    dsn = _dsn(tmp_path, aliases={"Front": "F.Cu"})
    seg = candidate.CopperItem("1", "segment", "A", "F.Cu", (1.0000004, 2.5), (3, 2.5), .25)
    via = candidate.CopperItem("2", "via", "A", "F.Cu", (3, 2.5), None, .6, .3)
    plan, catalog = _parse(session_text(dsn, (seg, via), {VIA: ViaSpec(.6, .3, ("F.Cu", "B.Cu"))}),
                           layers={"Front", "B.Cu"}, aliases={"Front": "F.Cu"})
    assert plan.tracks[0].layer == "F.Cu" and plan.tracks[0].points_mm == ((1.0, -2.5), (3.0, -2.5))
    assert plan.vias[0].position_mm == (3.0, -2.5) and plan.vias[0].spec == catalog[VIA]
    with pytest.raises(CapabilityError):  # a via size the exported DSN does not define
        session_text(dsn, (via,), {})
    with pytest.raises(CapabilityError):  # Specctra strings cannot carry a quote
        session_text(dsn, (candidate.CopperItem("3", "segment", 'N"1', "F.Cu", (0, 0), (1, 0), .2),), {})
