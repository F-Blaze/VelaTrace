"""Local, authored geometry benchmarks; never opens or modifies a user's board.

This corpus measures routing geometry, not impedance, return paths or SI. All
copper layers are explicitly signal layers; the dielectric is synthetic data.
"""
from dataclasses import asdict, replace
import hashlib
import json
import math
from pathlib import Path
import platform
import time
import uuid

from .candidate import (SafeCandidateValidator, canonical, context_matches,
                        project_context, trusted_via_catalog)
from .dsn import DsnInput, ExportTicket, file_digest
from .errors import ValidationError
from .freerouting import Freerouting, JAR_SHA256, VERSION, run_bounded
from .kicad_cli import KiCadCli
from .ses import parse_ses
from .sexpr import parse


def _id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'https://velatrace.invalid/benchmark/v1/' + name))


def fixture_texts(layer_count: int, *, dense: bool = False) -> dict[str, str]:
    """Deterministic crossed-bus corpus, hand-authored algorithmically under MIT."""
    if type(layer_count) is not int or layer_count not in (4, 6, 8):
        raise ValidationError('Benchmark layer count must be 4, 6 or 8.')
    if type(dense) is not bool:
        raise ValidationError('Dense fixture selection must be boolean.')
    layers = ('F.Cu', *(f'In{i}.Cu' for i in range(1, layer_count - 1)), 'B.Cu')
    count, pitch = (8, 2.5) if dense else (4, 6.0)
    pads = [(f'J{i+1}', f'N{n+1}', x, 20 + row * pitch)
            for i, (n, x, row) in enumerate(
                [(n, 15, n) for n in range(count)] +
                [(n, 65, count - 1 - n) for n in range(count)])]
    copper = '\n'.join(f'({0 if name == "F.Cu" else 2 if name == "B.Cu" else 2*(i+1)} "{name}" signal)'
                       for i, name in enumerate(layers))
    dielectric_nm, extra_nm = divmod(1_600_000 - 35_000 * layer_count, layer_count - 1)
    slabs = []
    for i, name in enumerate(layers):
        slabs.append(f'(layer "{name}" (type "copper") (thickness 0.035))')
        if i < len(layers)-1:
            dielectric = (dielectric_nm + (i < extra_nm)) / 1_000_000
            slabs.append(f'(layer "dielectric {i+1}" (type "core") (thickness {dielectric:.6f}) '
                         '(material "synthetic benchmark") (epsilon_r 4) (loss_tangent 0.02))')
    board = ['(kicad_pcb (version 20241229) (generator "velatrace_benchmark")',
             '(general (thickness 1.6)) (paper "A4")',
             f'(layers {copper} (13 "F.Paste" user) (1 "F.Mask" user) '
             '(25 "Edge.Cuts" user) (39 "F.CrtYd" user) (35 "F.Fab" user))',
             f'(setup (stackup {" ".join(slabs)}) (pad_to_mask_clearance 0))', '(net 0 "")']
    board.extend(f'(net {i+1} "N{i+1}")' for i in range(count))
    for ref, net, x, y in pads:
        board.append(f'''(footprint "VelaTrace:BenchmarkPad" (layer "F.Cu")
          (uuid "{_id(ref)}") (at {x} {y}) (attr smd exclude_from_pos_files exclude_from_bom)
          (property "Reference" "{ref}" (at 0 -1.5) (layer "F.Fab")
            (effects (font (size 0.5 0.5) (thickness 0.1))))
          (property "Value" "BenchmarkPad" (at 0 1.5) (layer "F.Fab")
            (effects (font (size 0.5 0.5) (thickness 0.1))))
          (fp_rect (start -0.9 -0.9) (end 0.9 0.9) (stroke (width 0.05) (type default))
            (fill none) (layer "F.CrtYd") (uuid "{_id(ref+'-court')}"))
          (pad "1" smd circle (at 0 0) (size 1.2 1.2) (layers "F.Cu" "F.Paste" "F.Mask")
            (net {int(net[1:])} "{net}") (uuid "{_id(ref+'-pad')}")))''')
    board.append(f'(gr_rect (start 5 5) (end 75 65) (stroke (width 0.05) (type default)) '
                 f'(fill none) (layer "Edge.Cuts") (uuid "{_id("edge")}")))')
    via = f'Via[0-{layer_count-1}]_600:300_um'
    dsn = ['(pcb crossed', '(parser (string_quote ") (space_in_quoted_tokens on))',
           '(resolution um 10) (unit um)', '(structure',
           *(f'(layer {name} (type signal) (property (index {i})))' for i, name in enumerate(layers)),
           '(boundary (path pcb 0 5000 -5000 75000 -5000 75000 -65000 5000 -65000 5000 -5000))',
           f'(via {via}) (rule (width 250) (clearance 200)))',
           '(placement (component BenchmarkPad',
           *(f'(place {ref} {x*1000:g} {-y*1000:g} front 0)' for ref, _, x, y in pads), '))',
           '(library (image BenchmarkPad (pin Pad 1 0 0))',
           '(padstack Pad (shape (circle F.Cu 1200)) (attach off))',
           f'(padstack {via} ' + ' '.join(f'(shape (circle {name} 600))' for name in layers) + ' (attach off)))',
           '(network',
           *(f'(net N{i+1} (pins J{i+1}-1 J{i+count+1}-1))' for i in range(count)),
           '(class Default ' + ' '.join(f'N{i+1}' for i in range(count)) +
           f' (circuit (use_via {via})) (rule (width 250) (clearance 200))))', '(wiring))']
    project = {'meta': {'filename': 'crossed.kicad_pro', 'version': 1},
               'board': {'design_settings': {'meta': {'version': 2}, 'drc_exclusions': [],
                   'rule_severities': {'missing_courtyard': 'warning',
                                      'track_not_centered_on_via': 'warning',
                                      'tuning_profile_track_geometries': 'warning',
                                      'footprint_filters_mismatch': 'warning',
                                      'footprint_type_mismatch': 'warning'},
                   'rules': {'min_clearance': .2, 'min_track_width': .2,
                             'min_via_diameter': .6, 'min_through_hole_diameter': .3},
                   'via_dimensions': [{'diameter': .6, 'drill': .3}]}},
               'net_settings': {'meta': {'version': 3}, 'classes': [{'name': 'Default',
                   'clearance': .2, 'track_width': .25, 'via_diameter': .6, 'via_drill': .3}]}}
    return {'crossed.kicad_pcb': '\n'.join(board), 'crossed.dsn': '\n'.join(dsn),
            'crossed.kicad_pro': json.dumps(project, indent=2)}


class _FixtureSafety:
    """Read-only saved-file adapter. Deliberately has no IPC or apply method."""
    def __init__(self, directory: Path):
        self.directory = directory

    def assert_matches(self, dsn, *, expected_board=None):
        dsn.assert_unchanged()
        text = dsn.ticket.board_path.read_text(encoding='utf-8')
        if expected_board is not None and canonical(parse(text, kicad=True)) != expected_board:
            raise ValidationError('Benchmark source changed during validation.')
        return text


def _save_new(path: Path, value: str):
    with path.open('x', encoding='utf-8', newline='\n') as output:
        output.write(value)


def run_benchmark(output: Path, *, jar: Path, java: Path, kicad_cli: Path,
                  layer_counts=(4, 6, 8), seconds: float = 120, dense: bool = False) -> dict:
    """Equal aggregate wall budget per baseline/portfolio, including validation.

    Child-tool timeouts bound individual work; validation/start-up are accounted
    for but cannot be preempted at the exact aggregate deadline. Overruns fail.
    Existing destinations are refused, so reruns cannot overwrite earlier data.
    """
    from .electrical_rules import ElectricalRules, NetRule, compile_electrical_dsn, validate_plan_rules
    from .portfolio import CandidateProducer, PortfolioBudget, explore_candidates
    from .route_cleanup import consolidate_collinear
    from .stackup import read_stackup
    if isinstance(seconds, bool) or not math.isfinite(seconds) or not 10 <= seconds <= 3600:
        raise ValidationError('Benchmark budget must be 10–3600 seconds per solver per fixture.')
    if not layer_counts or len(set(layer_counts)) != len(layer_counts):
        raise ValidationError('Specify distinct benchmark layer counts.')
    texts = [(n, fixture_texts(n, dense=dense)) for n in layer_counts]
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {'schema': 1, 'corpus': 'authored-crossed-bus-v1', 'electrical_validation': False,
                'limitations': ['Geometry-only corpus; no reference planes, impedance or delay approval.',
                                'No demonstrated superiority; this is a reproducible measurement harness.',
                                'Single-thread cold JVM, no configurable seed; repeat runs for timing comparisons.'],
                'platform': platform.platform(), 'python': platform.python_version(),
                'router': {'version': VERSION, 'sha256': JAR_SHA256, 'max_passes': 100, 'threads': 1},
                'seconds_per_solver': seconds, 'dense': dense, 'cases': []}
    config = output / 'kicad-config'
    config.mkdir()
    cli = KiCadCli(kicad_cli, timeout=min(120, seconds), config_directory=config)
    manifest['kicad_config'] = 'fresh isolated directory, no user config imported'
    manifest['kicad_version'] = cli.check_startup()
    preflight = Freerouting(jar, java, work_directory=output / 'preflight')
    preflight.check_startup()  # Missing/wrong tools are setup errors, not solver losses.
    manifest['runtime'] = {
        'java_sha256': file_digest(preflight.java),
        'java_version': run_bounded([str(preflight.java), '-version'], preflight.work_directory, 15).output,
        'kicad_cli_sha256': file_digest(cli.executable),
        'implementation_sha256': {path.name: file_digest(path) for path in sorted(Path(__file__).parent.glob('*.py'))}}
    for n, contents in texts:
        folder = output / f'{n}-layers'
        folder.mkdir()
        for name, text in contents.items():
            _save_new(folder / name, text)
        board, path = folder / 'crossed.kicad_pcb', folder / 'crossed.dsn'
        root = parse(contents['crossed.dsn'])
        from .sexpr import children, one
        layers = tuple(row[1] for row in children(one(root, 'structure'), 'layer'))
        nets = frozenset(row[1] for row in children(one(root, 'network'), 'net'))
        dsn = DsnInput(path, file_digest(path), ExportTicket.begin(board), nets, frozenset(layers),
                       base_design='crossed', placements={row[1]: (float(row[2])*.001, float(row[3])*.001, row[4], float(row[5]))
                       for row in children(one(one(root, 'placement'), 'component'), 'place')},
                       placement_resolution_mm=.0001)
        _, context = project_context(board)
        hashes = {name: file_digest(folder / name) for name in contents}
        stackup = read_stackup(contents['crossed.kicad_pcb'])
        case = {'layers': n, 'input_sha256': hashes, 'stackup_sha256': stackup.fingerprint, 'solvers': {}}
        manifest['cases'].append(case)
        def fresh():
            dsn.assert_unchanged()
            if not context_matches(context):
                raise ValidationError('Benchmark project/rules changed.')
        catalog = trusted_via_catalog(dsn)
        mandatory = ElectricalRules(tuple(NetRule(net, layers) for net in sorted(nets)))
        for solver in ('baseline', 'portfolio'):
            workspace = folder / solver
            workspace.mkdir()
            router = Freerouting(jar, java, work_directory=workspace, timeout_seconds=min(seconds, 300))
            validator = SafeCandidateValidator(_FixtureSafety(workspace), cli)
            emitted = 0
            validations = 0
            def produce(allowed):
                rules = ElectricalRules(tuple(NetRule(net, allowed) for net in sorted(nets)))
                def run(remaining):
                    nonlocal emitted
                    if remaining < 1:
                        raise ValidationError('Insufficient routing budget.')
                    emitted += 1
                    router.timeout = min(remaining, 300)
                    prefix = workspace / f'candidate-{emitted}'
                    started = time.monotonic()
                    attempt = {'rules': asdict(rules), 'timeout_seconds': router.timeout, 'status': 'failed',
                               'consolidate_collinear': solver == 'portfolio'}
                    router.last_log = ''
                    try:
                        compiled = compile_electrical_dsn(contents['crossed.dsn'], rules)
                        attempt['compiled_dsn_sha256'] = hashlib.sha256(compiled.encode()).hexdigest()
                        _save_new(prefix.with_suffix('.dsn'), compiled)
                        ses = router.route(dsn, (), electrical_rules=rules)
                        _save_new(prefix.with_suffix('.ses'), ses)
                        plan = parse_ses(ses, expected_design='crossed', nets=set(nets), layers=set(layers),
                                         via_catalog=catalog, expected_placements=dsn.placements,
                                         expected_placement_resolution_mm=dsn.placement_resolution_mm)
                        failures = validate_plan_rules(plan, rules, layers)
                        if failures:
                            raise ValidationError('; '.join(failures))
                        if solver == 'portfolio':
                            plan = consolidate_collinear(plan)
                        attempt['status'] = 'routed'
                        return plan
                    except Exception as exc:
                        attempt['error'] = f'{type(exc).__name__}: {exc}'
                        raise
                    finally:
                        attempt['wall_seconds'] = time.monotonic() - started
                        _save_new(prefix.with_suffix('.log'), router.last_log)
                        _save_new(prefix.with_suffix('.json'), json.dumps(attempt, indent=2))
                return run
            def validate(plan):
                nonlocal validations
                validations += 1
                started = time.monotonic()
                outcome = {}
                try:
                    failures = validate_plan_rules(plan, mandatory, layers)
                    report = validator.validate(dsn, plan, ())
                    if failures:
                        report = replace(report, blocking_reasons=(*report.blocking_reasons, *failures))
                    outcome['report'] = asdict(report)
                    return report
                except Exception as exc:
                    outcome['error'] = f'{type(exc).__name__}: {exc}'
                    raise
                finally:
                    outcome['wall_seconds'] = time.monotonic() - started
                    _save_new(workspace / f'validation-{validations}.json', json.dumps(outcome, indent=2,
                              default=lambda obj: sorted(obj) if isinstance(obj, (set, frozenset)) else str(obj)))
            producers = [CandidateProducer('all-signal-layers', produce(layers))]
            if solver == 'portfolio':
                producers += [CandidateProducer('outer-layers', produce(('F.Cu', 'B.Cu'))),
                              CandidateProducer('outer-and-first-inner', produce(('F.Cu', layers[1], 'B.Cu')))]
            start = time.monotonic()
            result = explore_candidates(producers, validate, budget=PortfolioBudget(len(producers), seconds),
                                        expected_board_digest=dsn.ticket.board_digest,
                                        required_constraint_ids=frozenset(), assert_fresh=fresh,
                                        cancelled=lambda: False)
            case['solvers'][solver] = {'result': asdict(result), 'wall_seconds': time.monotonic()-start}
            fresh()
            _save_new(workspace / 'result.json', json.dumps(case['solvers'][solver], indent=2,
                                                           default=lambda obj: sorted(obj) if isinstance(obj, (set, frozenset)) else str(obj)))
        case['source_unchanged'] = hashes == {name: file_digest(folder / name) for name in contents}
        if not case['source_unchanged']:
            raise ValidationError('Benchmark modified a source fixture.')
    _save_new(output / 'manifest.json', json.dumps(manifest, indent=2,
              default=lambda obj: sorted(obj) if isinstance(obj, (set, frozenset)) else str(obj)))
    return manifest
