"""External companion window and explicit read-only connectivity inspector."""
import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .netlist import read_xml_netlist


def main() -> None:
    parser = argparse.ArgumentParser(description="VelaTrace KiCad companion")
    parser.add_argument("--netlist", type=Path)
    parser.add_argument("--demo", action="store_true", help="Synthetic UI preview, no API or IPC")
    parser.add_argument("--screenshot", type=Path, help="Save the explicit demo window and exit")
    args = parser.parse_args()
    if args.netlist:
        print(json.dumps(asdict(read_xml_netlist(args.netlist)), default=str, indent=2))
    else:
        from .ui import launch
        raise SystemExit(launch(demo=args.demo, screenshot=args.screenshot))


if __name__ == "__main__":
    main()
