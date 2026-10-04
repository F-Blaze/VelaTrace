# Vela-routing

Vela-routing is the name of VelaTrace's experimental routing system. It combines
external routing engines with explicit constraints, independent checks and
measured candidate selection. Freerouting and KiCadRoutingTools retain their own
names, versions and licenses. The installed companion workflow still uses
Freerouting; this research branch has not been released or installed.

## Targets and evidence required

The targets are faster accepted routing, broader autonomous electrical design,
and demonstrated advantages over free routing tools. No finite corpus proves
universal superiority. Each comparison therefore names its corpus, engines,
constraints, budgets, acceptance rules and failures.

1. **Speed:** repeated wall-clock comparisons with equal required checks, rotating
   solver order, preparation included, and failed runs retained. A fast failure
   is not a successful route. A timing improvement on one corpus is not a general
   speed guarantee; engine-only times and full-pipeline times are distinct.
2. **Quality:** complete connectivity, no new/blocking DRC, and evidence for every
   supported electrical constraint. Compare copper length and vias only after
   the mandatory constraints pass. Never trade correctness for speed.
3. **Electrical autonomy:** explicit endpoint and fabrication requirements,
   physical stackup/materials, connected reference paths through transitions,
   calibrated impedance/timing and coupled-pair models, then validated tuning.
   Missing data remains unresolved; no guessed dielectric or automatic stackup
   change. User preview/approval and backup/undo remain mandatory for live writes.
4. **Breadth:** expand to independently held-out boards, dense inner-layer routes,
   escape routing, differential pairs, power and reference-plane cases. Compare
   the same problems across pinned engines before making an overall ranking.

Current measured results: [hybrid geometry benchmark](KRT_BENCHMARK.md) and
[reference-plane benchmark](REFERENCE_ROUTING.md). Neither is production
electrical sign-off. Rankings must state what is weighted; untested capability
must be marked untested rather than credited as either a win or a loss.

## Exact keepout compaction

The reference compiler can combine edge-adjacent triangulation pieces when their
union is convex. It uses shared-edge indexing with bounded passes/checks, and
requires the combined polygon union to equal the original forbidden region.
Holes and narrow slits cannot be simplified away. Every candidate still receives
fresh fill, independent full-width coverage and original-rule DRC.

`compile_reference_keepouts(..., merge_convex=True)` opts into compaction. The
default retains the prior triangle encoding because repeated results did not
demonstrate a consistent speed advantage. Fewer polygons are an input-complexity improvement;
they do not by themselves prove faster routing. Freerouting's route and runtime
can differ between equivalent obstacle decompositions.

The reference benchmark now runs plain Freerouting (`baseline`), the prior
triangle projection (`reference-triangles`) and Vela-routing's compact projection
(`vela-routing`). Add `--reference-repeats 3` to run three trials per layer count.
Manifest schema 2 reports preparation, projection, routing, DRC and fill timing,
full-pipeline totals and accepted counts. A paired speed ratio is reported only
when all paired old/new routes pass and at least three trials exist; this is an
observed ratio, not a statistical significance test. Tool startup checks are
excluded equally. The authored corpus remains the same small ground-notch case.

### Repeated native observations, 2026-10-04

Run `vela-routing-bench-01` executed all 27 solver/board trials (three repetitions
on each of 4, 6 and 8 layers). Both reference-aware encodings passed 9/9; plain
Freerouting crossed the required reference gap in 9/9. All routes passed ordinary
DRC with zero unconnected items; three pre-existing footprint warnings remained.
All source hashes were unchanged. Compaction reduced 596 keepouts to 33.

| Layers | Plain Freerouting median | Vela-routing triangles median | Vela-routing compact median |
|---|---|---|---|
| 4 | 14.09 s; 0/3 reference-valid | 18.84 s; 3/3 | 16.11 s; 3/3 |
| 6 | 12.20 s; 0/3 reference-valid | 15.00 s; 3/3 | 16.17 s; 3/3 |
| 8 | 13.69 s; 0/3 reference-valid | 23.47 s; 3/3 | 19.98 s; 3/3 |

These medians include common preparation and per-solver projection/validation;
they are not comparable directly to the earlier table's narrower timing scope.
The first four-layer trials overlapped the regression test run, and host timing
varied considerably. Paired old/new speed ratios were approximately 0.95, 0.78,
and 1.22 respectively (higher than 1 favors compact). The mixed, exploratory
results do not establish a general speedup, so compaction remains optional.
Raw measurements and input/tool/implementation hashes are retained in
[the manifest](benchmarks/vela-reference-2026-10-04.json).

The compact routes were 71.553–71.564 mm versus 71.533 mm for triangles and
50.000 mm for the uncovered baseline. No via was used. The new advisory computed
approximately 400–412 ps for the covered endpoint path depending on fixture
stackup and exact route; these are model estimates, not measured hardware delays.

Validation for this increment: 542 tests and 240 subtests passed, four optional
native tests skipped, Ruff clean. Native results above are separate real tool runs.

## Validation latency upgrade, 2026-10-04

The research reference-routing command now shares the strict candidate DRC
returned by official fresh zone filling. Previously it checked the candidate,
then checked it again during refill. The baseline still receives independent
DRC; the exact candidate bytes, project/rules context and KiCad version are
bound to the fresh result. Source freshness and copper geometry checks remain.
`RefilledCandidateValidator` supports empty constraints only and withholds the
writer evidence token: it cannot authorize a live board write. The production
validator and installed plugin are unchanged.

Controlled replay used the **same saved route** for each old/new pair, three
trials at each layer count, sequential arms and alternating order. No routing
engine was timed in this replay. Every arm passed DRC/connectivity, preserved
copper and covered the required reference plane: 9/9 old and 9/9 new. Source
hashes were unchanged. Three existing footprint warnings remained per case.

| Authored board | Previous validation + refill median | Fused validation + refill median | Reduction of medians |
|---|---|---|---|
| 4 layers | 5.125 s | 2.359 s | 54.0% |
| 6 layers | 4.234 s | 2.219 s | 47.6% |
| 8 layers | 4.422 s | 2.140 s | 51.6% |

These are validation savings, **not full routing speedups**. Small samples and
host/cache variation remain; the first old four-layer trial took 9.563 s. The
manifest also records the median of the paired percentage reductions, a
different statistic from reduction of medians. Raw observations are in
[the validation manifest](benchmarks/vela-validation-2026-10-04.json).

A second, sequential run of the committed replay script reproduced the result:
old/new medians were 3.297/2.203 s (4 layers), 3.812/1.937 s (6 layers) and
4.156/2.015 s (8 layers), reductions of 33.2%, 49.2% and 51.5%. It additionally
rechecked fresh-result DRC against a fresh source baseline after the timed arms.
Both arms again accepted 9/9, bringing the two replays to 18/18 per arm. The
[second manifest](benchmarks/vela-validation-repeat-2026-10-04.json) retains the
less favorable four-layer result as well as the faster cases. This confirms a
measured validation improvement, not a constant speed guarantee.

To reproduce after generating an archive with `--reference-repeats 3`, install
the `routing-research` dependencies and run:

```text
python scripts/benchmark_validation.py --archive <reference-output> --output <new-output> --kicad-cli <KiCad-10-cli>
```

The replay refuses source boards that differ from the exact authored fixtures.
It keeps the old implementation as a measurement arm. Native runs need to be
kept separate from regression tests and other routing jobs. Reference manifest
schema 3 identifies `fused-refill` and reports combined validation/refill time;
schema 2 had separate stage fields.

### Real-board evidence and remaining failures

A private local inventory found 44 byte-distinct boards, including 14 modern
4–8-layer files. Forty entries had projects with ignored DRC checks and eight had exclusions;
the existing strict validator refuses these settings. Most boards were already
routed. These counts establish available development data, not benchmark
successes. Held-out folders were not inspected and no designs were published.

Two small, unrouted two-layer projects had usable saved DSNs and unchanged
strict project rules. We copied their board/project/schematic context and
verified archived connectivity, layer membership and footprint placement
locally. Archived exports are **not fresh-export certification** of all DSN
geometry/settings. Six sequential trials per board used the same Freerouting
2.1.0 settings and 60-second router timeout, alternating old/new validation.
No candidate repair or KRT routing was used in this latency-isolation test.

| Private case | Previous accepted / attempts | Fused accepted / attempts | Previous / fused all-attempt median |
|---|---|---|---|
| Real board A | 0/3 | 0/3 | 32.05 / 31.84 s |
| Real board B | 3/3 | 2/3 | 21.36 / 18.53 s |

Times include shared preparation, routing and validation; common startup is
excluded. Failed routes remain in the medians, so **these times cannot establish
an accepted-routing speed win**. A still has clearance/shorting errors. One new
B attempt had dangling-track warnings; independent router attempts returned
different copper. Fused validation medians alone were 4.00 vs 6.94 s for A and
2.69 vs 4.45 s for B, but these were not identical-route pairs.

The first geometry checker compared full DSN paths with SES basenames and
incorrectly marked unchanged copper as changed. The correction recognizes only
the bound DSN's full name, basename or stem, retaining exact net/layer/width/via
geometry comparison. Every saved real candidate was freshly filled and checked
again; all preserved copper. Original observations remain private alongside a
separate corrected manifest. Additional recheck time is recorded separately and
excluded from the original timing measurements. Original user files remained
byte-identical. No overall router superiority or electrical sign-off is claimed.

Regression verification: 550 tests and 245 subtests passed, four optional native
tests skipped; Ruff passed. A fresh four-layer end-to-end run with the fused
validator accepted both reference-aware encodings and rejected the plain
baseline's reference gap; all three passed ordinary DRC/connectivity. Private
project names, paths, netlists and board
contents are excluded from this repository.

## Endpoint-specific delay advisory

`endpoint_delay.analyze_endpoint_delay` resolves explicit footprint/pad endpoints
and estimates delay along their connected route, using each segment's width and
the declared dielectric thickness/permittivity. Dead-end branch length and
unrelated net copper are not added to the source-to-destination delay. Reports
bind the board text, normalized stackup, route plan and requirement with hashes.

This first increment supports front-side, zero-rotation endpoint pads, F.Cu
tracks above adjacent In1.Cu, and a single explicitly declared dielectric slab.
Exact duplicate segments are deduplicated. Vias, layer transitions, missing
materials, ambiguous pins, cycles, overlapping segments and unrepresented
interior junctions produce `unknown`. The bounded topology check supports up to
512 distinct edges. It does not create or alter tracks.

The model remains the existing ideal zero-thickness, quasi-static microstrip
estimate. Real copper thickness, solder mask, losses, frequency dispersion,
coupling, manufacturing tolerance and reference-net attachment are not solved.
An `estimated` report is advisory and `electrically_verified` is always false.
The reference benchmark emits this advisory only for geometrically covered
candidates; its presence never changes route acceptance into electrical sign-off.

## Status

The name, exact keepout compaction, repeated comparison reporting and endpoint
delay advisory are implemented. Fully autonomous electrical design and broad
superiority remain open goals. No installed-plugin update or release is implied.
