"""Official CLI fallbacks. Only temporary reports/netlists are written here."""
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .errors import CapabilityError, ValidationError
from .models import DesignSnapshot
from .netlist import read_xml_netlist


def local_tool_environment() -> dict[str, str]:
    """Do not pass provider keys or IPC authentication to child tools."""
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
               "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "DISPLAY", "XAUTHORITY"}
    return {key: value for key, value in os.environ.items()
            if key.upper() in allowed or re.fullmatch(
                r"KICAD\d+_(?:FOOTPRINT|SYMBOL|3DMODEL|3RD_PARTY|TEMPLATE)_DIR", key)}


@dataclass(frozen=True)
class DrcResult:
    violations: int
    unconnected: int
    schematic_parity: int


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
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationError("KiCad DRC report is incomplete or malformed; approval is unavailable.") from exc
    return DrcResult(*(len(items) for items in rows))


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

    def _run(self, arguments: list[str], cwd: Path, allowed_exit_codes=(0,)) -> int:
        if self.version is None:
            self.check_startup()
        try:
            result = subprocess.run([str(self.executable), *arguments], cwd=cwd, shell=False,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=self.timeout, check=False, env=local_tool_environment(),
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise CapabilityError("KiCad CLI timed out; no verified result is available.") from exc
        except OSError as exc:
            raise CapabilityError("KiCad CLI could not run. Check its path and file permissions.") from exc
        if result.returncode not in allowed_exit_codes:
            raise ValidationError(f"KiCad CLI failed (exit {result.returncode}); inspect the design in KiCad.")
        return result.returncode

    def schematic_snapshot(self, schematic: Path, *, saved_confirmed: bool) -> DesignSnapshot:
        if not saved_confirmed:
            raise ValidationError("Save the schematic and confirm before exporting connectivity.")
        schematic = Path(schematic).resolve(strict=True)
        if schematic.suffix.lower() != ".kicad_sch":
            raise ValidationError("Select a KiCad schematic file.")
        with tempfile.TemporaryDirectory(prefix="velatrace-netlist-") as directory:
            output = Path(directory) / "netlist.xml"
            self._run(["sch", "export", "netlist", "--format", "kicadxml", "--output",
                       str(output), str(schematic)], schematic.parent)
            if not output.is_file():
                raise ValidationError("KiCad did not produce a connectivity netlist.")
            snapshot = read_xml_netlist(output)
            return replace(snapshot, source="kicad-cli-schematic", path=schematic,
                           warnings=("Analysis uses the saved schematic; unsaved edits are not included.",))

    def drc(self, candidate: Path) -> DrcResult:
        """Caller must supply a candidate copy with its matching project/rules files."""
        candidate = Path(candidate).resolve(strict=True)
        if candidate.suffix.lower() != ".kicad_pcb":
            raise ValidationError("DRC requires a KiCad candidate board.")
        with tempfile.TemporaryDirectory(prefix="velatrace-drc-") as directory:
            output = Path(directory) / "drc.json"
            status = self._run(["pcb", "drc", "--format", "json", "--severity-all",
                               "--all-track-errors", "--exit-code-violations", "--output",
                               str(output), str(candidate)], candidate.parent, (0, 5))
            if not output.is_file():
                raise ValidationError("KiCad did not produce a DRC report; approval is unavailable.")
            report = parse_drc_report(output)
            if status == 5 and not (report.violations or report.unconnected or report.schematic_parity):
                raise ValidationError("KiCad DRC status disagrees with its report; approval is unavailable.")
            return report
