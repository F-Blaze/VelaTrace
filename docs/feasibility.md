# Feasibility and boundary decisions

Verified 2026-09-20 against official documentation and installed PyPI kicad-python 0.8.0 source. Documentation at /kicad-python/ includes future KiCad 11 methods; their appearance is not evidence of availability in KiCad 9. No KiCad or Java is installed on this development host, so these are source checks, not end-to-end hardware/editor tests.

| Requirement | Verified capability and implementation decision |
| --- | --- |
| Embedded dock panel | No documented public IPC docking API. IPC plugins are external processes. Use a separate always-on-top window, positioned beside KiCad. |
| Temporary canvas previews | No temporary overlay API in pinned client. BoardShape/BoardText CRUD can add dedicated user-layer graphics. Every preview mutation needs the same backup boundary as routing; record exact IDs, remove on reject, and recover stale previews on restart. |
| Schematic connectivity | KiCad 9 IPC supports PCB only. Use saved schematic export: `kicad-cli sch export netlist --format kicadxml --output netlist.xml project.kicad_sch`. XML connectivity preserves pin-to-net membership. BOM alone is insufficient. Unsaved schematic edits require the human to save first. |
| PCB connectivity | Read footprint pads and each pad's net name through kipy. Do not infer connections from names, visual proximity, or a flat parts list. |
| Automatic DSN export / SES import | Pinned client has no DSN export or SES import method. KiCad 9 CLI also has no DSN export. v1 needs user-exported DSN, checked against current board geometry, or a future independently validated exporter. Do not call imaginary IPC methods. SES can be strictly parsed into supported track/via objects and applied with IPC, with unsupported constructs refused before mutation. |
| Undo transaction | `Board.begin_commit`, `push_commit`, and `drop_commit` are in 0.8.0 and documented as grouping edits into one undo step. Uncommitted edits do not render until push, so a long-lived open commit is not a preview mechanism. Live Ctrl+Z and IPC-disconnect failure tests remain release gates. |
| Theme / violet previews | IPC editor appearance settings are visibility/color modes, not a reliable OS-window light/dark theme getter. Read KiCad settings as a best effort or provide explicit light/dark selection. Per-copper-layer violet dashed preview cannot be guaranteed by current public IPC. Dedicated user-layer graphics remain subject to KiCad layer color; use a violet panel preview and disclose this limitation. Never recolor production copper or change stackup. |

## Version policy

Pin `kicad-python==0.8.0` (PyPI released 2026-08-30, MIT). It was generated against KiCad 10.0.6, but its `check_version()` accepts older servers. VelaTrace's baseline reader uses KiCad 9 board APIs and rejects versions below 9. Newer features must be checked by server version and actual response, never merely by `hasattr` on the newer client. KiCad 11 support is not certified by this source review. PyPI wheel SHA256 should be locked during release dependency hardening; this development pin alone is not supply-chain attestation.

## Primary sources

- [IPC add-on developers](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/) — external process, KiCad 9 PCB coverage, 9/10 export limitations.
- [IPC architecture](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/) — process boundary and protocol.
- [Official client releases](https://pypi.org/project/kicad-python/) — 0.8.0 release and version history.
- [Official client repository](https://gitlab.com/kicad/code/kicad-python) — inspected published 0.8.0 `kipy/board.py`, `board_types.py`, `kicad.py`, and generated version file locally.
- [KiCad 9 CLI](https://docs.kicad.org/9.0/en/cli/cli.html) — schematic netlist export, DRC and supported PCB exports.
- [Board reference](https://docs.kicad.org/kicad-python/board.html) — current API, version annotations must be respected.

## Architecture

`models.py` defines immutable connectivity snapshots. `ipc.py` is a read-only official IPC adapter. `netlist.py` parses KiCad XML connectivity and refuses flat BOMs. Audit state, provider calls, and routing state are independent services. All mutations, preview cleanup, backups and IPC commits must pass through one reviewed write-safety boundary. Freerouting is an external GPLv3 process; no router source or jar is bundled. The UI consumes service state rather than making classifications or arithmetic itself.
