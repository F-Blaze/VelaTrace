# Vela-routing development

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
- `route_repair.repair_dangling` removes only new route segments identified by
  official KiCad DRC as dangling. Trials preserve item identifiers, must retain
  full connectivity and introduce no new issue, and are bounded by pass/time
  limits. A final ordinary validation must prove the repaired plan clean. Vias,
  other warnings and unsupported SES geometry are never silently repaired.

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
first inner layer, with DRC-guided dangling-segment repair and without changing mandatory
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

After integration with main through `61daf7f` (one-click routing, audit updates,
and trimmed DRC library tables), the full regression suite passed **490 tests and
199 subtests**, with four optional native tests skipped. Lint passed. A separate
post-merge four-layer native rerun again correctly selected no winner: baseline
9.12 s, portfolio 29.58 s. The prototype has not been installed into the user's
production plugin or enabled in its UI.

## DRC-guided repair results, 2026-10-02

The next experiment replaces consolidation with the bounded `repair_dangling`
step described above. In two runs, the repaired portfolio produced a fully
connected, independently accepted candidate for every 4/6/8-layer fixture.
The single-run baseline produced no accepted candidate in either run.

| Signal layers | Run 1 portfolio | Run 2 portfolio | Run 2 copper length | Vias |
| --- | ---: | ---: | ---: | ---: |
| 4 | 41.27 s | 55.98 s | 235.618 mm | 8 |
| 6 | 36.72 s | 49.69 s | 233.910 mm | 8 |
| 8 | 47.27 s | 51.88 s | 237.659 mm | 8 |

Acceptance means zero unconnected items and zero new/blocking DRC issues under
the existing rules; eight footprint warnings already present on the synthetic
source board remain reported. Source boards, projects and DSNs stayed unchanged.
The second run's eight-layer baseline recorded a 1,711.98 s callback overrun and
was rejected for exceeding its 180 s budget. That timing outlier is not usable
as evidence of router speed. Other baseline attempts failed strict SES parsing
or retained new copper DRC issues. These results support the repair step on this
small corpus; they do not establish superiority over other routers, plane
continuity, differential matching or signal integrity. The baseline performs
one engine attempt while the portfolio may perform three within the same time
allowance, so the portfolio consumes more work on these easy fixtures.

Cleanup is deliberately absent from the live preview/approval workflow until
that integration is independently tested. In particular, validation must never
silently change a plan after its preview has already been displayed.

Final local validation of this milestone: **500 tests and 199 subtests passed**,
four optional native tests skipped, and lint clean. Native comparisons above ran
separately against installed tools; they are not simulated by the unit tests.

## Remaining engineering gates

The next reference-plane increment is implemented as an authored-fixture research
command: [REFERENCE_ROUTING.md](REFERENCE_ROUTING.md). It adds official fresh
zone filling, full-width vector coverage checks and projected router keepouts.
Native 4/6/8-layer notch tests pass; reference-net connectivity and via return
transitions remain unresolved. It is not integrated into the live routing UI.

The hybrid-engine milestone adds a pinned second external engine and fast/optimization
search policies. Setup, boundaries and measured comparisons are documented in
[KRT_BENCHMARK.md](KRT_BENCHMARK.md). It remains a synthetic research harness;
the live preview/approval path has not been switched to it.

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
