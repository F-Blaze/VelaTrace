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
