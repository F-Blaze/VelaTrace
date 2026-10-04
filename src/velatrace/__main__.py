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
    parser.add_argument("--benchmark", type=Path, help="Create a NEW local geometry benchmark directory; no IPC or AI")
    parser.add_argument("--reference-benchmark", type=Path, help="Create a NEW reference-plane notch benchmark; requires KiCad 10")
    parser.add_argument("--jar", type=Path, help="Pinned Freerouting 2.1.0 JAR for the benchmark")
    parser.add_argument("--java", type=Path, help="Java 21 executable for the benchmark")
    parser.add_argument("--kicad-cli", type=Path, help="KiCad 9+ CLI executable for the benchmark")
    parser.add_argument("--benchmark-seconds", type=float, default=120)
    parser.add_argument("--benchmark-layers", type=int, nargs='+', default=[4, 6, 8])
    parser.add_argument("--benchmark-dense", action="store_true")
    parser.add_argument("--benchmark-obstacles", action="store_true",
                        help="Add fixed no-net top-layer SMD copper obstacles to each synthetic case")
    parser.add_argument("--benchmark-krt", type=Path, help="Optional pinned KiCadRoutingTools 0.22.1 research checkout")
    parser.add_argument("--benchmark-krt-python", type=Path, help="Isolated Python with the pinned KRT dependencies")
    parser.add_argument("--benchmark-solvers", nargs='+', choices=('baseline', 'portfolio', 'krt', 'hybrid', 'hybrid-fast'),
                        help="Run only selected solvers; default is every configured solver")
    args = parser.parse_args()
    if args.reference_benchmark:
        if not all((args.jar, args.java, args.kicad_cli)) or args.benchmark or args.netlist or args.demo or args.screenshot:
            parser.error('--reference-benchmark requires --jar, --java and --kicad-cli and no other mode')
        from .reference_benchmark import run_reference_benchmark
        run_reference_benchmark(args.reference_benchmark, jar=args.jar, java=args.java, kicad_cli=args.kicad_cli,
                                seconds=args.benchmark_seconds, layer_counts=tuple(args.benchmark_layers))
        print(f'Reference benchmark complete: {args.reference_benchmark.resolve() / "manifest.json"}')
    elif args.benchmark:
        if not all((args.jar, args.java, args.kicad_cli)):
            parser.error('--benchmark requires --jar, --java and --kicad-cli')
        if args.netlist or args.demo or args.screenshot:
            parser.error('--benchmark cannot be combined with other modes')
        from .benchmark import run_benchmark
        run_benchmark(args.benchmark, jar=args.jar, java=args.java, kicad_cli=args.kicad_cli,
                      seconds=args.benchmark_seconds, layer_counts=tuple(args.benchmark_layers),
                      dense=args.benchmark_dense, obstacles=args.benchmark_obstacles,
                      krt_root=args.benchmark_krt, krt_python=args.benchmark_krt_python,
                      solvers=tuple(args.benchmark_solvers) if args.benchmark_solvers else None)
        print(f'Benchmark complete: {args.benchmark.resolve() / "manifest.json"}')
    elif args.netlist:
        print(json.dumps(asdict(read_xml_netlist(args.netlist)), default=str, indent=2))
    else:
        from .ui import launch
        raise SystemExit(launch(demo=args.demo, screenshot=args.screenshot))


if __name__ == "__main__":
    main()
