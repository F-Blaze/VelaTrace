"""Development entry point; replaced/extended by the UI phase."""
import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .netlist import read_xml_netlist


def main() -> None:
    parser = argparse.ArgumentParser(description="VelaTrace connectivity inspector")
    parser.add_argument("--netlist", type=Path)
    args = parser.parse_args()
    if args.netlist:
        print(json.dumps(asdict(read_xml_netlist(args.netlist)), default=str, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
