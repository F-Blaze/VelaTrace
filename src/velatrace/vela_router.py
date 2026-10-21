"""Route board adapter for VelaTrace's own router (vela_engine), no Java or JAR.

Honors the `Router` contract of routing.py: route(dsn, constraints) returns SES text,
so the result passes the same untrusted-output parser (nets, layers, trusted via
padstacks, base design), preview, candidate KiCad DRC and approval as before. It
routes the board copy KiCad exported the DSN from and only reads files.
"""
from dataclasses import replace
import importlib
from pathlib import Path, PureWindowsPath
import threading

from .errors import CapabilityError, ValidationError


def _quoted(name: str) -> str:
    # Specctra strings have no escapes: a quote inside a name cannot be written.
    if '"' in name:
        raise CapabilityError(f"Name {name!r} contains a quote; Vela-routing cannot write it to SES.")
    return f'"{name}"'


def session_text(dsn, items, via_catalog=None) -> str:
    """SES for KiCad-coordinate copper (mm, Y down): Specctra Y-up in whole nanometres,
    the DSN's own layer names and the DSN padstack name of each via size. No placement
    section: footprints are never moved."""
    dsn_layer = {canonical: name for name, canonical in dsn.layer_aliases.items()}
    def nm(value):
        return round(value * 1_000_000)
    nets = {}
    for item in items:
        if item.kind == "segment":
            (x1, y1), (x2, y2) = item.start, item.end
            row = (f"(wire (path {_quoted(dsn_layer.get(item.layer, item.layer))} {nm(item.width)} "
                   f"{nm(x1)} {nm(-y1)} {nm(x2)} {nm(-y2)}))")
        else:
            names = [name for name, spec in (via_catalog or {}).items()
                     if abs(spec.diameter_mm - item.width) < 1e-6 and abs(spec.drill_mm - item.drill) < 1e-6]
            if not names:
                raise CapabilityError(f"Vela-routing chose a {item.width:g}/{item.drill:g} mm via that the "
                                      "exported DSN does not define; nothing was routed.")
            row = f"(via {_quoted(sorted(names)[0])} {nm(item.start[0])} {nm(-item.start[1])})"
        nets.setdefault(item.net, []).append(row)
    design = PureWindowsPath(dsn.base_design or dsn.path.name).stem
    body = "\n".join(f"  (net {_quoted(net)}\n   " + "\n   ".join(rows) + ")" for net, rows in nets.items())
    return (f"(session {_quoted(design)}\n (base_design {_quoted(design)})\n (routes\n  (resolution um 1000)\n"
            f"  (network_out\n{body})))\n")


def constrained(board, constraints):
    """Raise every net class to the confirmed all-net minimum width/clearance."""
    width = max((c.minimum_mm for c in constraints if c.kind == "trace-width"), default=0)
    gap = max((c.minimum_mm for c in constraints if c.kind == "clearance"), default=0)
    board.classes = [replace(c, width=max(c.width, width), clearance=max(c.clearance, gap)) for c in board.classes]
    board.overrides = {net: replace(c, width=max(c.width, width), clearance=max(c.clearance, gap))
                       for net, c in board.overrides.items()}
    board.rules["min_track_width"] = max(board.rules.get("min_track_width", 0), width)  # also the neck width
    return board


class VelaRouter:
    def __init__(self, cli=None):
        self.cli = cli  # kicad-cli for copper-text obstacles; set once the board's KiCad is known.
        self.cancel = threading.Event()
        self.progress = lambda message: None
        self.last_report = None

    def check_startup(self) -> None:
        try:
            importlib.import_module("numpy")
        except ImportError:
            raise CapabilityError("Vela-routing needs numpy. Reinstall the plugin so KiCad installs "
                                  "requirements.txt.") from None

    def close(self) -> None:
        pass  # No process to stop.

    def route(self, dsn, constraints) -> str:
        from .candidate import trusted_via_catalog
        from .copper_text import extract_copper_text
        from .vela_board import load_board
        from .vela_engine import Options, kicad_check, route_board
        if not all(c.kind in {"clearance", "trace-width"} and c.target == "all nets" for c in constraints):
            raise CapabilityError("Some confirmed routing constraints cannot be enforced by this router.")
        # The private copy KiCad exported the DSN from (live board text); the saved
        # board only for a DSN the user exported by hand next to it.
        path = Path(dsn.path).with_suffix(".kicad_pcb")
        path = path if path.is_file() else dsn.ticket.board_path
        board = constrained(load_board(path), constraints)
        obstacles = ()
        if self.cli is not None:
            try:  # Copper text is only known through KiCad's own rendering.
                obstacles = extract_copper_text(path, self.cli).obstacles
            except (CapabilityError, ValidationError) as exc:
                self.progress(f"Copper text not modelled: {exc}")
        def log(text):
            self.progress("Routing · " + text.split("] ", 1)[-1])
        router = route_board(board, Options(log=log, cancel=self.cancel), obstacles)
        project, rules = path.with_suffix(".kicad_pro"), path.with_suffix(".kicad_dru")
        if not rules.is_file() and getattr(dsn, "ticket", None) is not None:
            rules = Path(dsn.ticket.board_path).with_suffix(".kicad_dru")
        if self.cli is not None and project.is_file():
            try:   # KiCad's own DRC of the zone-refilled copy: reconnect exactly what it finds open
                kicad_check(router, self.cli, path.name, (project, rules))
            except (CapabilityError, ValidationError) as exc:
                self.progress(f"KiCad check skipped: {exc}")
        self.last_report = router.report()
        items = router.copper()
        catalog = trusted_via_catalog(dsn) if any(item.kind == "via" for item in items) else {}
        return session_text(dsn, items, catalog)
