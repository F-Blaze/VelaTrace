"""Local paired replay: same authored routes, rotating order, no router timing claim."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
from statistics import median
from velatrace.candidate import trusted_via_catalog, route_issues
from velatrace.dsn import DsnInput, ExportTicket, file_digest
from velatrace.kicad_cli import KiCadCli
from velatrace.reference_benchmark import reference_fixture, validate_and_refill, refill_preserves_copper
from velatrace.reference_planes import ReferenceRequirement, check_reference_coverage
from velatrace.ses import parse_ses

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--archive', type=Path, required=True, help='Output of --reference-benchmark --reference-repeats 3')
parser.add_argument('--output', type=Path, required=True, help='New output directory')
parser.add_argument('--kicad-cli', type=Path, required=True)
args = parser.parse_args()
OUT = args.output.resolve()
OUT.mkdir(exist_ok=False)
cli = KiCadCli(args.kicad_cli, timeout=90, config_directory=OUT/'config')
version = cli.check_startup()
rows = []
manifest = {'scope': 'Validation and fresh fill only; identical archived authored routes; no routing speed claim',
            'kicad_version': version, 'kicad_cli_sha256': file_digest(cli.executable), 'cases': rows}
req = (ReferenceRequirement('N1', 'F.Cu', 'In1.Cu', 'GND', .05),)
for count in (4, 6, 8):
    folder = args.archive/f'{count}-layers-run-1'
    originals = reference_fixture(count)
    for name, expected in originals.items():
        if (folder/name).read_text(encoding='utf-8') != expected:
            raise ValueError('Replay accepts only the exact authored reference fixtures.')
    board = folder/'reference.kicad_pcb'
    dsn_path = folder/'vela-routing'/'reference.dsn'
    route = folder/'vela-routing'/'route.ses'
    hashes = {str(p.relative_to(folder)): file_digest(p) for p in [*(folder/n for n in originals), dsn_path, route]}
    layers = ('F.Cu', *(f'In{i}.Cu' for i in range(1,count-1)), 'B.Cu')
    dsn = DsnInput(dsn_path, file_digest(dsn_path), ExportTicket.begin(board), frozenset({'N1','GND'}), frozenset(layers),
                   'reference', {'J1':(15,-40,'front',0),'J5':(65,-40,'front',0),'J2':(10,-10,'front',0)}, .0001)
    catalog = trusted_via_catalog(dsn)
    plan = parse_ses(route.read_text(encoding='utf-8'), expected_design='reference', nets=set(dsn.nets), layers=set(layers),
                     via_catalog=catalog, expected_placements=dsn.placements, expected_placement_resolution_mm=.0001)
    case = {'layers':count, 'input_sha256': hashes, 'trials':[]}
    rows.append(case)
    # Verification outside timed arms; retain the raw observations before this
    # final comparison so additional validation never masquerades as faster routing.
    for trial in range(1,4):
        pair = {'trial':trial, 'arms':{}}
        case['trials'].append(pair)
        for fused in ((False,True) if trial%2 else (True,False)):
            arm = 'fused' if fused else 'separate'
            work = OUT/f'{count}-{trial}-{arm}'
            work.mkdir()
            report,fresh,elapsed = validate_and_refill(dsn,plan,cli,work,fused=fused)
            same = refill_preserves_copper(plan,fresh.text,dsn,via_catalog=catalog)
            coverage = check_reference_coverage(plan,fresh.text,req)
            accepted = (report.drc_violations==0 and report.unconnected_count==0 and not report.blocking_reasons
                        and same and coverage.status=='covered')
            pair['arms'][arm] = {'seconds':elapsed,'accepted':accepted,'report':asdict(report),'geometry_preserved':same,
                                  'coverage':coverage.status,'fresh_drc':asdict(fresh.drc)}
            print(json.dumps({'layers':count,'trial':trial,'arm':arm,'seconds':round(elapsed,3),'accepted':accepted}),flush=True)
    case['source_unchanged'] = all(file_digest(folder/name)==digest for name,digest in hashes.items())
    if not case['source_unchanged']:
        raise ValueError('Benchmark source changed during validation.')
    baseline = cli.refill_for_analysis(board).drc
    from velatrace.kicad_cli import DrcResult
    for pair in case['trials']:
        for row in pair['arms'].values():
            fresh_data = row['fresh_drc']
            fresh_drc = DrcResult(**fresh_data)
            blocking, _ = route_issues(baseline, fresh_drc)
            row['accepted'] = row['accepted'] and blocking == 0 and fresh_drc.unconnected == 0
    case['median_seconds'] = {arm:median(p['arms'][arm]['seconds'] for p in case['trials']) for arm in ('separate','fused')}
    case['paired_reduction_percent'] = (100*median(1-p['arms']['fused']['seconds']/p['arms']['separate']['seconds'] for p in case['trials'])
                                        if all(r['accepted'] for p in case['trials'] for r in p['arms'].values()) else None)
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2,default=lambda x:sorted(x) if isinstance(x,frozenset) else str(x)),encoding='utf-8')
print(json.dumps([{'layers':r['layers'],'median_seconds':r['median_seconds'],'paired_reduction_percent':r['paired_reduction_percent']} for r in rows]))
