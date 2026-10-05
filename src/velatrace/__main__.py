"""External companion window and explicit read-only connectivity inspector."""
import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .netlist import read_xml_netlist


def leave_working_directory() -> None:
    """Never run inside, or import from, the folder VelaTrace was started in: with
    `python -m velatrace` in a downloaded project that folder is first on the module
    path, and Windows looks there first for programs."""
    here = Path(__file__).resolve().parent
    cwd = Path.cwd().resolve()
    def untrusted(entry):
        try:
            return Path(entry or ".").resolve() == cwd != here.parent
        except OSError:
            return True
    sys.path[:] = [entry for entry in sys.path if not untrusted(entry)]
    os.chdir(here)


def main() -> None:
    parser = argparse.ArgumentParser(description="VelaTrace KiCad companion")
    parser.add_argument("--netlist", type=Path)
    parser.add_argument("--demo", action="store_true", help="Synthetic UI preview, no API or IPC")
    parser.add_argument("--screenshot", type=Path, help="Save the explicit demo window and exit")
    args = parser.parse_args()
    for name in ("netlist", "screenshot"):  # Relative to where the user typed them.
        if getattr(args, name):
            setattr(args, name, getattr(args, name).resolve())
    leave_working_directory()
    if args.netlist:
        print(json.dumps(asdict(read_xml_netlist(args.netlist)), default=str, indent=2))
    else:
        from .ui import launch
        raise SystemExit(launch(demo=args.demo, screenshot=args.screenshot))


if __name__ == "__main__":
    main()
