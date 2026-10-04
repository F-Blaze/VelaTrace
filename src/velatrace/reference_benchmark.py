"""Authored reference-notch experiment, with official fresh zone filling.

Never loads a user's board or invokes IPC. A longer route can be preferable when
the shortest geometric path crosses a required reference-plane void.
"""
from dataclasses import asdict
import json
import math
from pathlib import Path
import time

from .benchmark import _FixtureSafety, _id, _save_new, fixture_texts
from .candidate import SafeCandidateValidator, candidate_text, prepare_copper, route_issues, trusted_via_catalog
from .dsn import DsnInput, ExportTicket, file_digest
from .electrical_rules import ElectricalRules, NetRule, compile_electrical_dsn, validate_plan_rules
from .errors import ValidationError
from .external_copper import import_copper
from .freerouting import Freerouting, JAR_SHA256, VERSION
from .kicad_cli import KiCadCli
from .portfolio import candidate_geometry
from .reference_planes import ReferenceRequirement, check_reference_coverage
from .reference_routing import compile_reference_keepouts
from .ses import parse_ses
from .sexpr import QuotedAtom, children, one, parse, render


def reference_fixture(layers=4):
    """One F.Cu signal across a notched In1.Cu ground pour, with a ground PTH pad."""
    if type(layers) is not int or layers not in (4, 6, 8):
        raise ValidationError('Reference fixtures support 4, 6 or 8 copper layers.')
    original = fixture_texts(layers)
    root = parse(original['crossed.kicad_pcb'], kicad=True)
    kept = []
    positions = {'J1': (15, 40), 'J5': (65, 40), 'J2': (10, 10)}
    for row in root[1:]:
        if row[0] == 'net':
            if row[1] not in {'0', '1', '2'}:
                continue
            if row[1] == '2':
                row[2] = QuotedAtom('GND')
        if row[0] == 'footprint':
            ref = next(prop[2] for prop in children(row, 'property') if prop[1] == 'Reference')
            if ref not in positions:
                continue
            one(row, 'at')[1:] = [str(n) for n in positions[ref]]
            if ref == 'J2':
                one(row, 'attr')[1] = 'through_hole'
                pad = one(row, 'pad')
                pad[2] = 'thru_hole'
                one(pad, 'layers')[1:] = [QuotedAtom('*.Cu'), QuotedAtom('*.Mask')]
                one(pad, 'net')[2] = QuotedAtom('GND')
                pad.append(['drill', '0.3'])
        kept.append(row)
    notch = [(5.5, 5.5), (74.5, 5.5), (74.5, 64.5), (45, 64.5),
             (45, 18), (35, 18), (35, 64.5), (5.5, 64.5)]
    kept.append(['zone', ['net', '2'], ['net_name', QuotedAtom('GND')],
                 ['layer', QuotedAtom('In1.Cu')], ['uuid', QuotedAtom(_id('reference-zone'))],
                 ['hatch', 'edge', '0.5'], ['connect_pads', ['clearance', '0.2']],
                 ['min_thickness', '0.2'], ['fill', 'yes', ['thermal_gap', '0.3'], ['thermal_bridge_width', '0.3']],
                 ['polygon', ['pts', *[['xy', str(x), str(y)] for x, y in notch]]]])
    board = render(['kicad_pcb', *kept])
    layer_names = ('F.Cu', *(f'In{i}.Cu' for i in range(1, layers-1)), 'B.Cu')
    via = f'Via[0-{layers-1}]_600:300_um'
    circles = ' '.join(f'(shape (circle {layer} 600))' for layer in layer_names)
    gnd = ' '.join(f'(shape (circle {layer} 1200))' for layer in layer_names)
    dsn = f'''(pcb reference (parser (string_quote ") (space_in_quoted_tokens on))
      (resolution um 10) (unit um) (structure
      {' '.join(f'(layer {layer} (type signal) (property (index {i})))' for i, layer in enumerate(layer_names))}
      (boundary (path pcb 0 5000 -5000 75000 -5000 75000 -65000 5000 -65000 5000 -5000))
      (via {via}) (rule (width 250) (clearance 200)))
      (placement (component BenchmarkPad (place J1 15000 -40000 front 0) (place J5 65000 -40000 front 0))
                 (component GroundPad (place J2 10000 -10000 front 0)))
      (library (image BenchmarkPad (pin Pad 1 0 0)) (image GroundPad (pin Ground 1 0 0))
               (padstack Pad (shape (circle F.Cu 1200)) (attach off))
               (padstack Ground {gnd} (attach off)) (padstack {via} {circles} (attach off)))
      (network (net N1 (pins J1-1 J5-1)) (net GND (pins J2-1))
               (class Default N1 GND (circuit (use_via {via})) (rule (width 250) (clearance 200)))) (wiring))'''
    project = json.loads(original['crossed.kicad_pro'])
    project['meta']['filename'] = 'reference.kicad_pro'
    return {'reference.kicad_pcb': board, 'reference.kicad_pro': json.dumps(project, indent=2),
            'reference.dsn': dsn}


def run_reference_benchmark(output, *, jar, java, kicad_cli, layer_counts=(4, 6, 8), seconds=120):
    if (type(seconds) not in {int, float} or not math.isfinite(seconds) or not 10 <= seconds <= 3600
            or not layer_counts or len(set(layer_counts)) != len(layer_counts)):
        raise ValidationError('Reference benchmark needs distinct layer counts and a 10–3600 second budget.')
    cases = [(n, reference_fixture(n)) for n in layer_counts]
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    cli = KiCadCli(kicad_cli, timeout=min(seconds, 120), config_directory=output/'config')
    version = cli.check_startup()
    if version < (10, 0, 0):
        raise ValidationError('Native reference benchmark requires official KiCad 10 zone-refill CLI.')
    router = Freerouting(jar, java, work_directory=output/'router', timeout_seconds=seconds)
    router.check_startup()
    requirements = (ReferenceRequirement('N1', 'F.Cu', 'In1.Cu', 'GND', .05),)
    manifest = {'schema': 1, 'corpus': 'authored-reference-notch-v1', 'kicad_version': version,
                'router_version': VERSION, 'router_sha256': JAR_SHA256,
                'requirements': [asdict(r) for r in requirements], 'cases': [],
                'limitations': 'Geometric reference coverage only; not SI, impedance or return-transition approval.',
                'implementation_sha256': {p.name: file_digest(p) for p in Path(__file__).parent.glob('*.py')}}
    for count, files in cases:
        folder = output/f'{count}-layers'
        folder.mkdir()
        for name, content in files.items():
            _save_new(folder/name, content)
        source_hashes = {name: file_digest(folder/name) for name in files}
        board = folder/'reference.kicad_pcb'
        source_fill = cli.refill_for_analysis(board)
        _save_new(folder/'source-filled.kicad_pcb', source_fill.text)
        root = parse(files['reference.dsn'])
        layers = tuple(row[1] for row in children(one(root, 'structure'), 'layer'))
        policies = ElectricalRules(tuple(NetRule(net, ('F.Cu',)) for net in ('N1', 'GND')))
        compiled = compile_electrical_dsn(files['reference.dsn'], policies)
        projection = compile_reference_keepouts(compiled, source_fill.text, requirements)
        case = {'layers': count, 'input_sha256': source_hashes, 'keepouts': projection.keepout_count,
                'source_fill': {'source_digest': source_fill.source_digest, 'context_digest': source_fill.context_digest},
                'solvers': {}}
        manifest['cases'].append(case)
        for name, text in (('baseline', compiled), ('reference-aware', projection.dsn_text)):
            work = folder/name
            work.mkdir()
            path = work/'reference.dsn'
            _save_new(path, text)
            dsn = DsnInput(path, file_digest(path), ExportTicket.begin(board), frozenset({'N1', 'GND'}),
                           frozenset(layers), 'reference',
                           {'J1': (15, -40, 'front', 0), 'J5': (65, -40, 'front', 0), 'J2': (10, -10, 'front', 0)}, .0001)
            started = time.monotonic()
            outcome = {'accepted': False}
            router.last_log = ''
            try:
                ses = router.route(dsn, ())
                _save_new(work/'route.ses', ses)
                catalog = trusted_via_catalog(dsn)
                plan = parse_ses(ses, expected_design='reference', nets=set(dsn.nets), layers=set(layers),
                                 via_catalog=catalog, expected_placements=dsn.placements,
                                 expected_placement_resolution_mm=.0001)
                failures = validate_plan_rules(plan, policies, layers)
                if failures:
                    raise ValidationError('; '.join(failures))
                validator = SafeCandidateValidator(_FixtureSafety(work), cli)
                report = validator.validate(dsn, plan, ())
                # Keep this deliverable separate from the immutable source board.
                candidate = work/'reference.kicad_pcb'
                _save_new(candidate, candidate_text(files['reference.kicad_pcb'], prepare_copper(plan, dsn)))
                _save_new(work/'reference.kicad_pro', files['reference.kicad_pro'])
                fresh = cli.refill_for_analysis(candidate)
                _save_new(work/'fresh-filled.kicad_pcb', fresh.text)
                # Check that native serialization/refill preserved routed copper,
                # including net assignments. Refilled files are never applied.
                filled_root = parse(fresh.text, kicad=True)
                without_copper = render([filled_root[0], *(r for r in filled_root[1:]
                                       if not isinstance(r, list) or r[0] not in {'segment', 'via', 'arc'})])
                restored = import_copper(without_copper, fresh.text, dsn, via_catalog=catalog)
                if candidate_geometry(restored)[0] != candidate_geometry(plan)[0]:
                    raise ValidationError('Native refill changed route geometry or net assignment.')
                coverage = check_reference_coverage(plan, fresh.text, requirements)
                final_blocking, final_warnings = route_issues(source_fill.drc, fresh.drc)
                elapsed = time.monotonic()-started
                outcome.update(report=asdict(report), coverage=asdict(coverage), coverage_status=coverage.status,
                               fresh_drc={'blocking': final_blocking, 'preexisting_warnings': final_warnings,
                                          'unconnected': fresh.drc.unconnected},
                               metrics=asdict(candidate_geometry(plan)[1]),
                               accepted=(report.drc_violations == 0 and report.unconnected_count == 0
                                         and not report.blocking_reasons and coverage.status == 'covered'
                                         and final_blocking == 0 and fresh.drc.unconnected == 0
                                         and elapsed < seconds))
            except Exception as exc:
                outcome['error'] = f'{type(exc).__name__}: {exc}'
            outcome['wall_seconds'] = time.monotonic()-started
            _save_new(work/'router.log', router.last_log)
            _save_new(work/'result.json', json.dumps(outcome, indent=2, default=list))
            case['solvers'][name] = outcome
        case['source_unchanged'] = source_hashes == {name: file_digest(folder/name) for name in files}
        if not case['source_unchanged']:
            raise ValidationError('Reference benchmark source changed.')
    _save_new(output/'manifest.json', json.dumps(manifest, indent=2, default=list))
    return manifest
