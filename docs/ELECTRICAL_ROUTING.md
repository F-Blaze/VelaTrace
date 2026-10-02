# Electrical routing development

The objective is autonomous routing that can demonstrate improvements over open-source
baselines on public, reproducible tests. No superiority claim is currently supported.
Freerouting already supports more than two layers. The work here adds engineering
constraints, independent verification and measured candidate selection around it.

## First milestone: implemented development APIs

- `stackup.read_stackup` reads ordered KiCad copper/dielectric slabs, including
  dielectric sublayers. Missing material properties remain unknown. A physical
  fingerprint invalidates derived data when the cross-section changes.
- `stackup.thin_microstrip` and `homogeneous_delay_ps` are explicitly idealized
  estimates. They do not establish impedance compliance, via delay, differential
  coupling, manufacturing tolerance or signal integrity on a real board.
- `electrical_rules.compile_electrical_dsn` compiles per-net permitted track layers
  and layer widths into dedicated Specctra classes. It preserves inherited rules
  and refuses conflicting reductions. Direct net-level `layer_rule` is ignored by
  pinned Freerouting 2.1.0, so that encoding is never used.
- `electrical_rules.validate_plan_rules` independently checks supported candidate
  geometry. A via barrel passing a reserved plane is different from routing a
  signal track on that plane. Clearance also requires actual candidate DRC.
- `portfolio.explore_candidates` explores a bounded list locally, deduplicates
  quantized geometry, and admits only complete candidates with zero blocking DRC
  and complete constraint evidence. Ranking is total unique track length, then
  unique vias. It revalidates the winner so mutable writer evidence cannot belong
  to a different candidate. Cancellation and changed inputs invalidate results.
- `route_cleanup.consolidate_collinear` creates a candidate with exactly the same
  quantized copper coverage using fewer overlapping collinear track objects. Its
  output still requires DRC; equal copper coverage does not guarantee that KiCad
  considers the new object endpoints valid.

These APIs and the benchmark are experimental. The companion window retains its
existing routing workflow; these additions do not claim electrical approval in the
GUI and do not relax its backup/preview/approval/undo safeguards.

## Run the geometry benchmark

From a development environment with VelaTrace installed:

```sh
python -m velatrace --benchmark NEW_OUTPUT_DIRECTORY --jar PATH_TO_FREEROUTING_2_1_0_JAR --java PATH_TO_JAVA_21 --kicad-cli PATH_TO_KICAD_CLI
```

All three executable paths are required. The output directory must not already
exist. No API key, network service or open KiCad editor is needed. The harness
creates its own synthetic boards and never reads a user's board.

The generated corpus has crossed buses on 4, 6 and 8 signal layers. Use
`--benchmark-dense` for eight connections instead of four,
`--benchmark-layers 4` for a smaller run, and `--benchmark-seconds 120` to set the
same aggregate wall-time allowance for each solver on each board. All copper
layers are signal layers: this corpus does **not** test ground/power planes or SI.
The synthetic stackup material is explicitly declared, not inferred FR-4.

The baseline uses the pinned external Freerouting process once. The portfolio
tries all permitted signal layers, outer layers only, and outer layers plus the
first inner layer, with candidate consolidation and without changing mandatory
design rules. Both receive the
same wall-time budget, including validation and final winner revalidation.
Cold JVM mode and a single routing thread are fixed. Startup and DRC are bounded
individually; a callback can exceed the remaining aggregate time before control
returns, in which case no winner is accepted. Time all failures as well as wins.

The manifest records source hashes, physical-stackup fingerprint, tool version,
configuration, per-candidate outcomes, length/via metrics, DRC evidence, timings,
and source preservation. Runtime/source hashes and compiled DSN inputs are included.
SES outputs, failure records and local router logs are retained. KiCad uses a new
isolated configuration directory so personal settings are not imported. Repeat
trials for timing comparisons: the pinned backend has no verified seed interface.
An empty winner is a failed run, not a completed board. Keep result directories
private if later extending the harness with confidential designs.

## Initial measurements, 2026-10-01

The local regression suite passed 356 tests plus 166 subtests, with three opt-in
native tests skipped; lint passed. A preceding full run hit an existing 50 ms
network-watchdog test timing failure under load; its isolated rerun and subsequent
full suite passed. Native benchmark results below are separate real engine/DRC
runs, using Freerouting 2.1.0 and a 180-second allowance per solver/fixture:

| Signal layers | Baseline elapsed | Portfolio elapsed | Accepted winner |
| --- | ---: | ---: | --- |
| 4 | 7.72 s | 24.77 s | Neither |
| 6 | 9.45 s | 30.45 s | Neither |
| 8 | 7.20 s | 25.80 s | Neither |

Candidates that parsed connected all fixture nets but still had dangling-track
warnings (and sometimes off-center via warnings), which correctly blocked
acceptance. Some 6/8-layer outputs also contained a single-point SES path; the
strict parser refused that unsupported geometry. Consolidation alone did not
solve these failures. Every source board/project/DSN remained unchanged.
This trial demonstrates working refusal/measurement, **not** better routing.
It identifies endpoint normalization and a better candidate backend as work still
needed before comparing electrical performance. These tiny synthetic fixtures
cannot establish performance on engineering designs.

## Remaining engineering gates

1. Import confirmed endpoint-specific electrical requirements and fabrication
   limits, with units, provenance and a binding to board/project/stackup hashes.
   Never assume every multilayer board requires one particular plane arrangement.
2. Check actual filled reference copper, voids, connected islands and return-path
   transitions. Layer names and net names are insufficient evidence.
3. Evaluate source-to-sink electrical paths using layer delays, actual used via
   spans and package delays. Branched-net total copper length is not path delay.
4. Add coupled-pair constraints, impedance tolerance corners and length/delay
   tuning using a validated backend. Unsupported physics must remain unresolved.
5. Audit and isolate additional backends; reject footprint moves, pin/net swaps,
   unexpected zone changes and rule relaxation. Never import legacy SWIG plugins.
6. Expand to held-out obstacle, escape, power and reference-plane corpora, then
   compare pinned backends under equal budgets. Only after evidence review should
   the selected strategy enter the confirmed GUI preview/approval flow.

## Primary references and licensing

- [KiCad physical stackup format](https://dev-docs.kicad.org/en/file-formats/sexpr-pcb/)
  and [KiCad 10 stackup implementation](https://github.com/KiCad/kicad-source-mirror/blob/10.0/pcbnew/board_stackup_manager/board_stackup.cpp).
- [KiCad 10 PCB manual](https://docs.kicad.org/10.0/en/pcbnew/pcbnew.html): timing
  profiles include layer and via delay; cached delays need refreshing after
  stackup edits. KiCad DRC alone does not prove signal integrity.
- [Qucs microstrip equations](https://qucs.sourceforge.net/tech/node75.html):
  Hammerstad–Jensen model and its domain. Numerical formula accuracy is not the
  accuracy of a fabricated board; solder mask, finite copper and dispersion matter.
- [Freerouting 2.1.0 Network.java](https://github.com/freerouting/freerouting/blob/v2.1.0/src/main/java/app/freerouting/designforms/specctra/Network.java),
  [NetClass.java](https://github.com/freerouting/freerouting/blob/v2.1.0/src/main/java/app/freerouting/designforms/specctra/NetClass.java),
  and [Circuit.java](https://github.com/freerouting/freerouting/blob/v2.1.0/src/main/java/app/freerouting/designforms/specctra/Circuit.java)
  document the class-level restrictions used by the compiler.
- Candidate comparators: [KiCadRoutingTools](https://github.com/drandyhaas/KiCadRoutingTools),
  [rsixel's fork](https://github.com/rsixel/KiCadRoutingPlacementTools),
  [tscircuit autorouter](https://github.com/tscircuit/tscircuit-autorouter),
  [Topola](https://github.com/mikwielgus/topola). Their published feature claims
  still need independent tests against the same inputs and constraints.

VelaTrace's new code and authored fixture generator are MIT. Freerouting remains
an unmodified, separately installed GPLv3 external process. No GPL router source
is copied into or linked with VelaTrace.
