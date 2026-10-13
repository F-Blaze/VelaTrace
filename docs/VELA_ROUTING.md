# Vela-routing

Vela-routing is the name of VelaTrace's experimental routing system. It combines
external routing engines with explicit constraints, independent checks and
measured candidate selection. Freerouting and KiCadRoutingTools retain their own
names, versions and licenses. Since 2026-10-12 it also has a native engine of
its own, described next. The installed companion workflow still uses Freerouting;
none of this is wired into the plugin or released.

## Native engine (experimental, 2026-10-12)

Vela-routing now has its own router: `vela_board.py` (reads a saved board
directly), `vela_engine.py` (the router) and `vela_electrical.py` (stackup
estimates). It is research code, needs `numpy` (the `routing-research` extra),
and is **not wired into the plugin**. Every result is checked by KiCad DRC, which
stays the only judge. It is an independent implementation from published ideas;
no code was taken from other routers.

```text
python -m velatrace.vela_engine board.kicad_pcb --out <new folder> --kicad-cli <KiCad-10-cli>
```

The command never touches the board it is given. It writes a routed copy, an
unrouted copy and `report.json` into the new folder.

### How it works

- **Board in, no DSN.** Pads (including custom shapes and hole offsets), holes,
  board outline, rule areas, copper graphics, net classes and the stackup are read
  from the `.kicad_pcb` and `.kicad_pro`. Pad positions were compared with KiCad's
  own on 12 of the 13 boards below and matched exactly; KiCad's Python did not
  load the thirteenth (mikoto) within 45 s. Copper text is taken from KiCad's rendering through the
  existing `copper_text` extractor.
- **Grid search with exact clearances.** A uniform grid, snapped to the finest
  pad pitch, with 45-degree moves and through vias. Each net class has its own
  occupancy map, so wide and narrow nets keep their own clearance. Via size is
  raised to the board's minimums.
- **Negotiated congestion, then a hard pass.** Nets first route through each
  other at a rising price and with a growing history penalty around conflicts.
  Whatever still overlaps is re-routed around everything else or dropped, so the
  output never contains two nets in conflict by the engine's own maps.
- **Tidy-up and rescue.** Each net is re-routed by plain shortest path and kept
  only if not worse. A dropped connection is retried by moving up to four
  blocking nets aside, accepted only when more connections result.
- **Neck-down.** A class width is treated as a default: a pad the class width
  cannot leave gets a short escape at the board minimum (0.15 mm floor), reported
  as `necked_mm`.
- **Restarts.** Up to four re-route orders are tried inside the time budget and
  the best is kept. The order is seeded; run-to-run repeatability was not measured.

### 4+ layers and electrical behaviour

- **Reference planes.** An inner layer that is at least 30 % copper zone is kept
  as a solid plane. No signal is routed on it. Surface pads of the plane's net
  drop their own via into it (or join copper within about 1 mm that already
  does); through-hole pads inside the plane count as connected. As a last resort
  a dropped connection may use a plane layer, and the length is reported as
  `signal_on_plane_mm`.
- **Dielectric constant and delay.** Thickness and permittivity come from the
  board stackup. Each signal layer is classed as microstrip, stripline, embedded
  or unreferenced from the planes actually present. A grid step costs its
  propagation delay, so the search minimises delay, not millimetres, and slower
  buried layers are used only when they pay. The report gives delay in ps per
  net, impedance per layer for the routed width, and the width that would give
  50 ohm on each layer.
- **Missing stackup data** is filled with generic FR-4 (permittivity 4.5,
  0.2 mm outer dielectric on 4+ layers, 35 um copper) and every affected figure
  is flagged `assumed`. This replaces "no guessed dielectric" for this engine
  only; nothing is written back to the board.
- **Pairs.** Nets named like `USB_D+`/`USB_D-` are routed first and their delay
  skew is reported. They are **not** routed as coupled pairs.

All figures are quasi-static estimates (the existing Hammerstad-Jensen
microstrip and the IPC-2141 stripline approximation). They are not a field
solver and not signal-integrity sign-off.

### Measured on the 13-board set, 2026-10-12

Same stripped open-source boards as the 2026-10-04 comparison. Accepted means
what the plugin means: KiCad DRC with refilled zones reports nothing unconnected
and no error or new warning. Times are wall-clock with three or four boards
running at once on one PC and include the restarts. One run per board.

| Board (layers) | Connections | Left open | New DRC issues | Accepted | Time |
|---|---|---|---|---|---|
| practice (2) | 8/8 | 0 | 0 | yes | 5 s |
| Prod_v2 (2) | 15/15 | 0 | 0 | yes | 14 s |
| tp4056-power-path (2) | 8/8 | 0 | 0 | yes | 1 s |
| anvil-stm32f031 (2) | 117/117 | 0 | 0 | yes | 106 s |
| esp32-4ch-relays (2) | 154/154 | 0 | 0 | yes | 96 s |
| mtb-led-atmega328pb (2) | 222/222 | 0 | 0 | yes | 221 s |
| usb-i2c-bridge (2) | 57/57 | 0 | 0 | no: 4 starved-thermal errors already on the unrouted board | 17 s |
| bms-buck-boost (2) | 88/89 | 1 | 0 | no: also 109 errors already on the unrouted board | 201 s |
| zereader-pico (2) | 221/223 | 2 | 2 (teardrop in a keep-out, starved thermal) | no | 318 s |
| rp2040-breakout (4) | 107/108 | 1 | 0 | no | 183 s |
| soleil (4) | 176/179 | 3 | 3 (2 connection-width warnings, 1 courtyard) | no | 304 s |
| mikoto (4) | 161/166 | 6 | 1 (a via already on the board left dangling) | no | 209 s |
| bitaxe-ultra (4) | 280/289 | 9 | 1 (unidentified) | no | 414 s |

Six of 13 accepted; seven of 13 fully connected with no new DRC issue. **No
4-layer board is fully routed yet**: they end at 97 to 99 % with clean copper.

For context only, the record of 2026-10-04 for the Freerouting path on the same
boards (900 s cap, not re-run today, so not a side-by-side test): accepted
practice; 85.7 % on usb-i2c-bridge; no plan within the cap on anvil, bms,
esp32-4ch-relays and rp2040-breakout; refused tp4056, bitaxe, zereader and
mtb-led at import; mikoto and soleil not measured. Prod_v2 was rejected then;
main's smoke test now expects it accepted. This is 13 boards, one run
each, on one PC. It supports "routes more of this set", not a general ranking.

### Known limits

- No coupled differential-pair routing, no length or delay tuning (serpentines),
  no impedance-driven width changes: delay, impedance and skew are reported, and
  delay steers layer choice, but nothing is tuned to a target.
- Through vias only. No blind, buried or micro vias, no BGA fanout patterns.
- Custom rule files (`.kicad_dru`), footprint copper text, teardrops and
  thermal-relief spoke rules are not modelled; KiCad DRC catches the misses.
- Existing tracks are obstacles, not part of the net. Existing vias are
  connected like pads.
- Outer-layer copper pours are not trusted to connect pads; those nets are
  routed with tracks.
- Speed: the search loop is pure Python, roughly 100,000 steps per second.
  Dense boards take minutes. A compiled kernel is the obvious next step.
- The grid costs density: with 0.2 mm tracks and 0.13 mm clearance, tracks pack
  at 0.4 mm, not 0.33 mm.
- Tested on Windows with KiCad 10.0.6 only. `tests/test_vela_engine.py` covers
  loader geometry, a brute-force clearance check, plane drops and neck-down.

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

### Private development evidence

Private-board measurements are retained in local workspace reports. Public
comparisons in this repository use only authored fixtures. Neither corpus
establishes universal superiority or full electrical sign-off.

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

The quality-focused copper-text increment adds official-rendering-based obstacle
projection and strict native via-default compatibility. Its authored 4/6/8-layer
surface-routing regression accepted 9/9 Vela routes versus 0/9 plain baseline
routes. See [scope, failures and reproducible evidence](COPPER_TEXT_ROUTING.md).
This is a targeted quality result, not universal superiority or an installation.

The name, exact keepout compaction, repeated comparison reporting and endpoint
delay advisory are implemented. Fully autonomous electrical design and broad
superiority remain open goals. No installed-plugin update or release is implied.
