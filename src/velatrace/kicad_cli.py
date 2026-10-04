"""Official CLI fallbacks. Only temporary reports/netlists are written here."""
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import weakref

from .errors import CapabilityError, ExportUnavailable, RoutingCancelled, ValidationError
from .models import DesignSnapshot
from .netlist import read_xml_netlist
from .sexpr import QuotedAtom, parse, render


def local_tool_environment() -> dict[str, str]:
    """Do not pass provider keys or IPC authentication to child tools."""
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
               "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "DISPLAY", "XAUTHORITY",
               "KICAD_CONFIG_HOME"}
    return {key: value for key, value in os.environ.items()
            if key.upper() in allowed or re.fullmatch(
                r"KICAD\d+_(?:FOOTPRINT|SYMBOL|3DMODEL|3RD_PARTY|TEMPLATE)_DIR", key)}


def user_config_dir(version: tuple[int, int, int]) -> Path:
    """The settings folder kicad-cli reads when VelaTrace does not override it."""
    root = os.environ.get("KICAD_CONFIG_HOME")
    if not root:
        if os.name == "nt":
            root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "kicad"
        elif sys.platform == "darwin":
            root = Path.home() / "Library" / "Preferences" / "kicad"
        else:
            root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kicad"
    return Path(root) / f"{version[0]}.{version[1]}"


_FONT_FACE = re.compile(r'\(face "(?:[^"\\]|\\.)*"\)')
_FOOTPRINT_ID = re.compile(r'\(footprint\s+"((?:[^"\\]|\\.)*)"')


def footprint_libraries(board_text: str) -> frozenset[str]:
    """Library nicknames of the board's footprints. A superset is harmless (it only
    loads more), so a loose scan is safe; KiCad nicknames never contain ':'."""
    ids = (re.sub(r"\\(.)", r"\1", value) for value in _FOOTPRINT_ID.findall(board_text))
    return frozenset(value.split(":", 1)[0] for value in ids if ":" in value)


def trimmed_library_table(table: Path, keep: frozenset[str], folder: Path, depth: int = 0) -> str:
    """The fp-lib-table limited to `keep` nicknames; nested tables are trimmed into
    `folder`. KiCad 10 DRC loads every listed library for its footprint-library
    checks (~10 s for the stock libraries), yet only the board's own libraries can
    change the result. Raises ValueError when the table cannot be trimmed exactly."""
    root = parse(table.read_text(encoding="utf-8"), kicad=True)
    if root[0] != "fp_lib_table" or depth > 4:
        raise ValueError(table)
    rows = [root[0]]
    for row in root[1:]:
        if isinstance(row, list) and row and row[0] == "lib":
            fields = {item[0]: item[1] for item in row[1:] if isinstance(item, list) and len(item) == 2}
            if str(fields.get("type", "")).lower() == "table":
                uri = str(fields.get("uri", ""))
                if "$" in uri or not Path(uri).is_absolute():
                    raise ValueError(uri)  # Path variables resolve inside KiCad only.
                nested = folder / f"fp-lib-table-{depth}-{len(rows)}"
                nested.write_text(trimmed_library_table(Path(uri), keep, folder, depth + 1), encoding="utf-8")
                row = [item if not (isinstance(item, list) and item[:1] == ["uri"])
                       else ["uri", QuotedAtom(nested.as_posix())] for item in row]
            elif fields.get("name") not in keep:
                continue
        rows.append(row)
    return render(rows) + "\n"


@dataclass(frozen=True)
class DrcResult:
    violations: int
    unconnected: int
    schematic_parity: int
    # (type, severity, item uuids) per violation/parity issue, to tell pre-existing
    # issues from ones a route introduced. Empty means identities are unknown.
    issues: tuple = ()


def _issue(row) -> tuple | None:
    items = row.get("items", [])
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError()
    kind, severity = row.get("type"), row.get("severity")
    uuids = [item.get("uuid") for item in items]
    # Missing identifiers are not evidence that two warnings concern the same
    # items. Keep their counts, but make the baseline comparison fail closed.
    if (not isinstance(kind, str) or not kind or not isinstance(severity, str) or not severity
            or not uuids or any(not isinstance(value, str) or not value for value in uuids)):
        return None
    return kind, severity, tuple(sorted(uuids))


def parse_drc_report(path: Path) -> DrcResult:
    if path.stat().st_size > 32_000_000:
        raise ValidationError("KiCad DRC report exceeds 32 MB.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError()
        rows = [value[key] for key in ("violations", "unconnected_items", "schematic_parity")]
        if any(not isinstance(items, list) or any(not isinstance(item, dict) for item in items)
               for items in rows):
            raise ValueError()
        ignored = value.get("ignored_checks", [])
        if not isinstance(ignored, list):
            raise ValueError()
        if ignored:
            raise ValidationError("KiCad reports disabled DRC checks; enable them before route approval.")
        if "included_severities" in value:
            severities = value["included_severities"]
            if (not isinstance(severities, list) or
                    any(not isinstance(item, str) for item in severities) or
                    not {"error", "warning", "exclusion"}.issubset(severities)):
                raise ValidationError("KiCad omitted DRC severities; a complete report is required.")
        identities = [_issue(row) for row in (*rows[0], *rows[2])]
        issues = () if any(issue is None for issue in identities) else tuple(sorted(identities))
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationError("KiCad DRC report is incomplete or malformed; approval is unavailable.") from exc
    return DrcResult(*(len(items) for items in rows), issues)


class KiCadCli:
    def __init__(self, executable: str | Path = "kicad-cli", timeout: float = 120):
        found = shutil.which(str(executable))
        if not found:
            raise CapabilityError("kicad-cli is missing. Install KiCad 9+ and configure its executable path.")
        self.executable = Path(found).resolve(strict=True)
        if os.name == "nt" and self.executable.suffix.lower() != ".exe":
            raise CapabilityError("Select the native kicad-cli.exe, not a shell script.")
        if not isinstance(timeout, (float, int)) or not 0 < timeout <= 600:
            raise ValidationError("KiCad CLI timeout must be between 0 and 600 seconds.")
        self.timeout = timeout
        self.version: tuple[int, int, int] | None = None
        self._exportable: set[tuple] = set()  # (schematic, project) digests proven exportable
        self._export_lock = threading.Lock()
        self._config_homes: dict[frozenset, Path] = {}  # footprint libraries -> DRC settings folder
        self._config_lock = threading.Lock()
        self.python: Path | None = None  # KiCad's bundled Python; found next to kicad-cli when unset
        self._export_home: Path | None = None

    def check_startup(self) -> tuple[int, int, int]:
        try:
            result = subprocess.run([str(self.executable), "version"], shell=False,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=10, check=False, env=local_tool_environment(),
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CapabilityError("Cannot run kicad-cli. Check the configured KiCad installation.") from exc
        text = result.stdout[:4096].decode("utf-8", errors="replace")
        match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", text)
        if result.returncode or not match or int(match[1]) < 9:
            raise CapabilityError("VelaTrace requires a working kicad-cli version 9 or newer.")
        self.version = tuple(int(part) for part in match.groups())
        return self.version

    def require_editor_version(self, editor_version: tuple[int, int, int]):
        actual = self.check_startup()
        if actual != tuple(editor_version):
            expected = ".".join(map(str, editor_version))
            found = ".".join(map(str, actual))
            raise CapabilityError(f"KiCad CLI {found} does not match editor {expected}. "
                                  "Configure the CLI from the same KiCad installation before routing.")

    def _run(self, arguments: list[str], cwd: Path, allowed_exit_codes=(0,), config_home=None) -> int:
        if self.version is None:
            self.check_startup()
        environment = local_tool_environment()
        if config_home is not None:
            environment["KICAD_CONFIG_HOME"] = str(config_home)
        try:
            result = subprocess.run([str(self.executable), *arguments], cwd=cwd, shell=False,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=self.timeout, check=False, env=environment,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise CapabilityError("KiCad CLI timed out; no verified result is available.") from exc
        except OSError as exc:
            raise CapabilityError("KiCad CLI could not run. Check its path and file permissions.") from exc
        if allowed_exit_codes is not None and result.returncode not in allowed_exit_codes:
            raise ValidationError(f"KiCad CLI failed (exit {result.returncode}); inspect the design in KiCad.")
        return result.returncode

    def schematic_snapshot(self, schematic: Path, *, saved_confirmed: bool) -> DesignSnapshot:
        if not saved_confirmed:
            raise ValidationError("Save the schematic and confirm before exporting connectivity.")
        schematic = Path(schematic).resolve(strict=True)
        if schematic.suffix.lower() != ".kicad_sch":
            raise ValidationError("Select a KiCad schematic file.")
        with tempfile.TemporaryDirectory(prefix="velatrace-netlist-", ignore_cleanup_errors=True) as directory:
            output = Path(directory) / "netlist.xml"
            self._run(["sch", "export", "netlist", "--format", "kicadxml", "--output",
                       str(output), str(schematic)], schematic.parent)
            if not output.is_file():
                raise ValidationError("KiCad did not produce a connectivity netlist.")
            snapshot = read_xml_netlist(output)
            return replace(snapshot, source="kicad-cli-schematic", path=schematic,
                           warnings=("Analysis uses the saved schematic; unsaved edits are not included.",))

    def _prove_exportable(self, schematic: Path) -> None:
        """KiCad may skip parity if it cannot fetch a schematic netlist. Prove that
        the saved context is exportable before requesting that check. The proof is
        a function of the exact schematic and project bytes, so it runs once per
        content, not once per candidate copy (concurrent DRCs wait for it)."""
        project = schematic.with_suffix(".kicad_pro")
        key = tuple(hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
                    for path in (schematic, project))
        with self._export_lock:
            if key not in self._exportable:
                self.schematic_snapshot(schematic, saved_confirmed=True)
                self._exportable.add(key)

    def _drc_config_home(self, libraries: frozenset[str]) -> Path:
        """A private copy of the user's KiCad settings whose footprint table lists
        only `libraries`, reused for every DRC of the same board. The user's own
        settings folder is only read."""
        with self._config_lock:
            home = self._config_homes.get(libraries)
            if home is not None:
                return home
            if self.version is None:
                self.check_startup()
            home = Path(tempfile.mkdtemp(prefix="velatrace-kicad-settings-"))
            weakref.finalize(self, shutil.rmtree, home, True)
            source, target = user_config_dir(self.version), home / f"{self.version[0]}.{self.version[1]}"
            target.mkdir()
            for name in ("kicad_common.json", "sym-lib-table"):  # path variables; symbol libraries
                if (source / name).is_file():
                    shutil.copyfile(source / name, target / name)
            table = source / "fp-lib-table"
            if table.is_file():
                try:
                    text = trimmed_library_table(table, libraries, target)
                except (OSError, ValueError, ValidationError):
                    text = table.read_text(encoding="utf-8")  # Exact, only slower.
                (target / "fp-lib-table").write_text(text, encoding="utf-8")
            # KiCad writes its default settings files on first use; do that once here,
            # so concurrent DRCs never read a half-written file. No board: it just exits.
            self._run(["pcb", "drc", str(target / "none.kicad_pcb")], home, None, home)
            self._config_homes[libraries] = home
            return home

    def drc(self, candidate: Path) -> DrcResult:
        """Caller must supply a candidate copy with its matching project/rules files."""
        candidate = Path(candidate).resolve(strict=True)
        if candidate.suffix.lower() != ".kicad_pcb":
            raise ValidationError("DRC requires a KiCad candidate board.")
        parity = []
        schematic = candidate.with_suffix(".kicad_sch")
        if schematic.exists():
            self._prove_exportable(schematic)
            parity = ["--schematic-parity"]
        home = self._drc_config_home(footprint_libraries(candidate.read_text(encoding="utf-8")))
        with tempfile.TemporaryDirectory(prefix="velatrace-drc-", ignore_cleanup_errors=True) as directory:
            output = Path(directory) / "drc.json"
            # --refill-zones: copper pours are filled in memory (the candidate file is
            # not saved), so pour-connected pads count as connected and pour clearances
            # are checked against the new tracks, exactly as after "Fill All Zones".
            status = self._run(["pcb", "drc", "--format", "json", "--severity-all", "--refill-zones",
                               "--all-track-errors", "--exit-code-violations", "--output",
                               str(output), *parity, str(candidate)], candidate.parent, (0, 5), home)
            if not output.is_file():
                raise ValidationError("KiCad did not produce a DRC report; approval is unavailable.")
            report = parse_drc_report(output)
            if status == 5 and not (report.violations or report.unconnected or report.schematic_parity):
                raise ValidationError("KiCad DRC status disagrees with its report; approval is unavailable.")
            return report

    def kicad_python(self) -> Path | None:
        """The Python that ships with this KiCad (it has the pcbnew module)."""
        if self.python is not None:
            return self.python
        folder = self.executable.parent
        for path in (folder / "python.exe", folder / "python3",  # Windows bundle
                     folder.parent / "Frameworks" / "Python.framework" / "Versions" / "Current" / "bin" / "python3"):
            if path.is_file():
                return path
        # Linux packages install pcbnew into the system Python.
        found = shutil.which("python3") if os.name != "nt" and sys.platform != "darwin" else None
        return Path(found) if found else None

    def _export_config_home(self) -> Path:
        """Private settings for the export process: only path variables are copied, so
        KiCad never writes (or reads unrelated state from) the user's own settings."""
        with self._config_lock:
            if self._export_home is None:
                if self.version is None:
                    self.check_startup()
                home = Path(tempfile.mkdtemp(prefix="velatrace-kicad-export-"))
                weakref.finalize(self, shutil.rmtree, home, True)
                target = home / f"{self.version[0]}.{self.version[1]}"
                target.mkdir()
                source = user_config_dir(self.version) / "kicad_common.json"
                if source.is_file():
                    shutil.copyfile(source, target / source.name)
                self._export_home = home
            return self._export_home

    def export_dsn(self, board_text: str, board_path: Path, folder: Path, cancel=None) -> Path:
        """Write the live board text and its saved project into `folder`, then export
        Specctra DSN with KiCad's own exporter in a separate process. The user's board,
        project and settings are only read. Raises ExportUnavailable when this KiCad
        cannot export headlessly (no bundled Python/pcbnew, e.g. SWIG removed)."""
        python = self.kicad_python()
        if python is None:
            raise ExportUnavailable("KiCad's bundled Python was not found next to kicad-cli.")
        board_path, folder = Path(board_path), Path(folder)
        board = folder / board_path.name
        # KiCad's Python takes minutes to load a board whose text uses a font that is
        # not installed (measured: >100 s against 0.5 s). The export copy only feeds
        # the DSN, where a font matters solely for the size of a copper-text keepout,
        # so it falls back to KiCad's built-in font. DRC always uses the real text.
        board.write_text(_FONT_FACE.sub("", board_text), encoding="utf-8")
        project = board_path.with_suffix(".kicad_pro")
        if project.is_file():  # Net classes live in the project; the DSN carries them.
            shutil.copyfile(project, board.with_suffix(".kicad_pro"))
        output = board.with_suffix(".dsn")
        environment = local_tool_environment()
        environment["KICAD_CONFIG_HOME"] = str(self._export_config_home())
        try:
            process = subprocess.Popen([str(python), "-I", "-c", EXPORT_SCRIPT, str(board), str(output)],
                                       cwd=folder, env=environment, shell=False, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            raise ExportUnavailable("KiCad's bundled Python could not start.") from exc
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                _, error = process.communicate(timeout=.2)
                break
            except subprocess.TimeoutExpired:
                if (cancel is not None and cancel.is_set()) or time.monotonic() > deadline:
                    process.kill()
                    process.communicate()
                    if cancel is not None and cancel.is_set():
                        raise RoutingCancelled("Cancelled; nothing was written to the board.") from None
                    raise ExportUnavailable(f"DSN export exceeded {self.timeout:g} seconds and was stopped.") from None
        if process.returncode or not output.is_file():
            tail = error[-2000:].decode("utf-8", errors="replace")
            reason = ("this KiCad's Python has no pcbnew module" if "pcbnew" in tail and "Error" in tail
                      else f"exit {process.returncode}")
            raise ExportUnavailable(f"KiCad could not export the DSN ({reason}).")
        return output


# Arguments, not formatted source: paths never become code.
# KiCad's exporter leaves copper text and graphics out of the DSN, so Freerouting
# routes straight through them (real boards: tracks shorting a name written on
# B.Cu). Each one gets a no-tracks/no-vias rule area on the in-memory board, which
# the exporter writes as a keepout. The temporary board is never saved.
# ponytail: bounding boxes, so a long diagonal copper line blocks its whole
# rectangle; use the item's outline if that ever costs a routable board.
EXPORT_SCRIPT = """import sys, pcbnew
board = pcbnew.LoadBoard(sys.argv[1])
try:
    for item in list(board.GetDrawings()):
        if pcbnew.IsCopperLayer(item.GetLayer()):
            box = item.GetBoundingBox()
            area = pcbnew.ZONE(board)
            area.SetLayer(item.GetLayer())
            area.SetIsRuleArea(True)
            area.SetDoNotAllowTracks(True)
            area.SetDoNotAllowVias(True)
            outline = area.Outline()
            outline.NewOutline()
            for x, y in ((box.GetLeft(), box.GetTop()), (box.GetRight(), box.GetTop()),
                         (box.GetRight(), box.GetBottom()), (box.GetLeft(), box.GetBottom())):
                outline.Append(x, y)
            board.Add(area)
except Exception:
    pass  # Candidate DRC still rejects a route that touches copper graphics.
sys.exit(0 if pcbnew.ExportSpecctraDSN(board, sys.argv[2]) else 3)"""
