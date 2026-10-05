"""Authored quality regression for copper text omitted from a DSN export."""
from dataclasses import asdict
import json
import math
from pathlib import Path

from .benchmark import _id, _save_new, _FixtureSafety
from .candidate import route_issues, trusted_via_catalog, SafeCandidateValidator
from .copper_text import prepare_copper_text_dsn
from .dsn import DsnInput, ExportTicket, file_digest
from .errors import ValidationError
from .electrical_rules import compile_electrical_dsn, ElectricalRules, NetRule
from .freerouting import Freerouting, VERSION, JAR_SHA256
from .kicad_cli import KiCadCli
from .portfolio import candidate_geometry
from .reference_benchmark import reference_fixture, validate_and_refill, refill_preserves_copper
from .ses import parse_ses
from .route_repair import repair_dangling
from .sexpr import QuotedAtom, children, one, parse, render


def copper_text_fixture(layers):
    files = reference_fixture(layers)
    root = parse(files['reference.kicad_pcb'], kicad=True)
    root = [root[0], *(row for row in root[1:] if row[0] != 'zone')]
    root.append(['gr_text', QuotedAtom('I'), ['at', '40', '40', '0'], ['layer', QuotedAtom('F.Cu')],
                 ['uuid', QuotedAtom(_id('copper-text-obstacle'))],
                 ['effects', ['font', ['size', '10', '10'], ['thickness', '0.6']]]])
    files['reference.kicad_pcb'] = render(root)
    return files


def run_copper_text_benchmark(output, *, jar, java, kicad_cli, layer_counts=(4, 6, 8), repeats=3, seconds=120,
                             surface_only=False):
    """Equal engine/settings, original-rule DRC, rotated order; timing not a goal."""
    if (type(surface_only) is not bool or type(repeats) is not int or not 1 <= repeats <= 9 or not layer_counts
            or len(set(layer_counts)) != len(layer_counts)
            or type(seconds) not in {int, float} or not math.isfinite(seconds) or not 10 <= seconds <= 3600):
        raise ValidationError('Copper benchmark requires 1–9 repeats, distinct layers and a 10–3600 s budget.')
    cases = [(count, copper_text_fixture(count)) for count in layer_counts]
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    cli = KiCadCli(kicad_cli, timeout=min(seconds, 120), config_directory=output/'config')
    version = cli.check_startup()
    if version < (10, 0, 0):
        raise ValidationError('Fresh-fill quality benchmarks require official KiCad CLI 10 or newer.')
    router = Freerouting(jar, java, work_directory=output/'router', timeout_seconds=seconds)
    router.check_startup()
    manifest = {'schema': 1, 'corpus': 'authored-copper-text-v1', 'scope': 'Quality, not runtime or SI sign-off',
                'kicad_version': version, 'router_version': VERSION, 'router_sha256': JAR_SHA256,
                'kicad_cli_sha256': file_digest(cli.executable), 'java_sha256': file_digest(router.java),
                'router_timeout_seconds': seconds, 'repeats': repeats, 'cases': [],
                'surface_only': surface_only, 'vela_cleanup': 'Existing DRC-guided dangling-segment repair, max 60 s',
                'implementation_sha256': [{'file': p.name, 'sha256': file_digest(p)}
                                           for p in sorted(Path(__file__).parent.glob('*.py'))]}
    for count, files in cases:
        if surface_only:
            files['reference.dsn'] = compile_electrical_dsn(files['reference.dsn'], ElectricalRules(
                tuple(NetRule(net, ('F.Cu',)) for net in ('N1', 'GND'))))
        folder = output/f'{count}-layers'
        folder.mkdir()
        for name, text in files.items():
            _save_new(folder/name, text)
        hashes = {name: file_digest(folder/name) for name in files}
        board, path = folder/'reference.kicad_pcb', folder/'reference.dsn'
        layers = frozenset(row[1] for row in children(one(parse(files['reference.dsn']), 'structure'), 'layer'))
        dsn = DsnInput(path, file_digest(path), ExportTicket.begin(board), frozenset({'N1', 'GND'}), layers,
                       'reference', {'J1': (15, -40, 'front', 0), 'J5': (65, -40, 'front', 0),
                                     'J2': (10, -10, 'front', 0)}, .0001)
        protected = prepare_copper_text_dsn(dsn, cli, folder/'protected')
        baseline = cli.refill_for_analysis(board)
        catalog = trusted_via_catalog(dsn)
        case = {'layers': count, 'input_sha256': hashes, 'obstacles': len(protected.evidence.obstacles),
                'text_items': protected.evidence.text_items, 'trials': []}
        manifest['cases'].append(case)
        for trial in range(1, repeats+1):
            arms = [('freerouting', dsn), ('vela-routing', protected.dsn)]
            if trial % 2 == 0:
                arms.reverse()
            for name, route_input in arms:
                work = folder/f'{trial}-{name}'
                work.mkdir()
                row = {'trial': trial, 'arm': name, 'accepted': False}
                router.last_log = ''
                try:
                    protected.assert_current(cli)
                    ses = router.route(route_input, ())
                    _save_new(work/'route.ses', ses)
                    plan = parse_ses(ses, expected_design=dsn.base_design, nets=set(dsn.nets), layers=set(layers),
                                     via_catalog=catalog, expected_placements=dsn.placements,
                                     expected_placement_resolution_mm=dsn.placement_resolution_mm)
                    if surface_only and any(track.layer != 'F.Cu' for track in plan.tracks):
                        raise ValidationError('Router violated the authored surface-only rule.')
                    if name == 'vela-routing':
                        repaired = repair_dangling(plan, route_input, (), SafeCandidateValidator(_FixtureSafety(work), cli),
                                                  timeout_seconds=min(seconds, 60))
                        row['cleanup'] = {'removed_segments': repaired.removed_segments, 'passes': repaired.passes,
                                          'details': repaired.details}
                        plan = repaired.plan
                    report, fresh, _ = validate_and_refill(route_input, plan, cli, work)
                    preserved = refill_preserves_copper(plan, fresh.text, route_input, via_catalog=catalog)
                    blocking, _ = route_issues(baseline.drc, fresh.drc)
                    protected.assert_current(cli)
                    row.update(report=asdict(report), fresh_blocking=blocking, geometry_preserved=preserved,
                               metrics=asdict(candidate_geometry(plan)[1]),
                               accepted=(preserved and blocking == 0 and fresh.drc.unconnected == 0
                                         and report.drc_violations == 0 and report.unconnected_count == 0
                                         and not report.blocking_reasons))
                    _save_new(work/'fresh-filled.kicad_pcb', fresh.text)
                except Exception as exc:
                    row['error'] = f'{type(exc).__name__}: {exc}'
                _save_new(work/'router.log', router.last_log)
                _save_new(work/'result.json', json.dumps(row, indent=2, default=list))
                case['trials'].append(row)
        protected.assert_current(cli)
        case['source_unchanged'] = hashes == {name: file_digest(folder/name) for name in files}
        if not case['source_unchanged']:
            raise ValidationError('Copper benchmark changed an authored source.')
        case['accepted'] = {name: sum(row['accepted'] for row in case['trials'] if row['arm'] == name)
                            for name in ('freerouting', 'vela-routing')}
    _save_new(output/'manifest.json', json.dumps(manifest, indent=2, default=list))
    return manifest


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jar', type=Path, required=True)
    parser.add_argument('--java', type=Path, required=True)
    parser.add_argument('--kicad-cli', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--layers', type=int, nargs='+', default=[4, 6, 8])
    parser.add_argument('--surface-only', action='store_true', help='Constrain both engines to F.Cu to isolate text avoidance')
    args = parser.parse_args()
    result = run_copper_text_benchmark(args.output, jar=args.jar, java=args.java, kicad_cli=args.kicad_cli,
                                       repeats=args.repeats, layer_counts=tuple(args.layers), surface_only=args.surface_only)
    print(json.dumps([{'layers': c['layers'], 'accepted': c['accepted']} for c in result['cases']]))
