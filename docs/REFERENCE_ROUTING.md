# Experimental reference-plane routing

VelaTrace can steer an external router around missing reference-plane copper,
then independently check the complete trace width against a freshly filled
candidate. This is a research API and an authored-fixture command. It is not yet
an option in the companion window or an electrical sign-off system.

The useful distinction from shortest-path routing is demonstrated by the notch
fixture: a 50 mm straight connection passes ordinary KiCad DRC but crosses a gap
in its required ground reference. Projected keepouts make Freerouting take a
71.533 mm detour with continuous geometric coverage instead.

## Approach and prior work

We inspected the MIT
[KiCadRoutingTools 0.22.1 reference diagnostic](https://github.com/drandyhaas/KiCadRoutingTools/blob/023d3f79027d5406e4ea8e68291f588c5c135673/py_tools/check_impedance.py).
Its sampled raster checks motivated a separate, independently implemented vector
check. VelaTrace requires an explicit signal net/layer and adjacent reference
net/layer, checks every full edge plus half-width and a specified margin, and
reports missing or ambiguous evidence as unknown. A 0.001 mm notch is covered by
the regression tests; no sampling interval can skip it.

Only actual `filled_polygon` copper counts. Zone outlines, layer names and
generic GND labels are insufficient. KiCad's zero-width reversed-edge bridges
around holes are reconstructed conservatively using polygonized faces and the
original contour's even/odd interior. Unrelated crossings and unmatched bridges
are refused. This follows KiCad's documented
[fractured polygon representation](https://docs.kicad.org/doxygen/classSHAPE__POLY__SET.html);
no generic polygon repair is used to invent copper.

Missing copper is projected into layer-specific DSN keepouts. Multiple reference
requirements on one signal layer intersect the supported region, conservatively
restricting all nets on that layer. Shapely 2.1.1
[constrained triangulation](https://shapely.readthedocs.io/en/2.1.1/reference/shapely.constrained_delaunay_triangles.html)
preserves void boundaries; the triangle union must equal the intended exclusion.
Router quantization and approximate margin buffering make this steering, not
proof: the final candidate always receives an independent vector check.

Freerouting 2.1.0 remains an unmodified external GPLv3 process. No upstream router
source or binary is copied into VelaTrace. New VelaTrace code and fixtures are MIT.
Shapely is a pinned optional research dependency and part of the test environment.

## Fresh fills and write safety

[KiCad 10's official CLI](https://docs.kicad.org/10.0/en/cli/cli.html#_pcb_drc)
supports `--refill-zones` before DRC and `--save-board` for the resulting fill.
Ordinary zone-bearing candidate DRC now requests refill without saving. For
geometry inspection, VelaTrace copies the board and matching project/rules into
a private directory, backs up that copy, then refills/saves only the copy. Source
and project hashes must remain unchanged. The benchmark also checks that native
serialization preserved all routed segment/via geometry and net assignments.

KiCad 9 lacks these official CLI refill options. Zone-bearing candidate DRC
therefore refuses with an actionable capability error on KiCad 9. Zone-free DRC
and the other supported KiCad 9 workflows remain available. No new SWIG/pcbnew
integration is used for filling.

## Reproduce

Use a reviewed development checkout and a separate environment; this does not
create a signed release or update an installed plugin. Install the research extra
alongside the usual dependencies, then supply the pinned external tools:

```sh
python -m pip install -e '.[routing-research]'
python -m velatrace --reference-benchmark NEW_RESULTS_DIRECTORY --benchmark-layers 4 6 8 --benchmark-seconds 180 --jar PATH_TO_FREEROUTING_2_1_0_JAR --java PATH_TO_JAVA_21 --kicad-cli PATH_TO_KICAD_10_CLI
```

The output directory must be new. The command creates its own PCB/DSN/project
fixtures, fills their ground zones locally, and compares the same engine with
and without projected keepouts. It never opens a user board, uses IPC, or calls
an AI provider. There is no API charge. Runtime manifests include input and
implementation hashes, routing logs, SES files, fresh candidate boards, DRC,
coverage evidence and preserved-source checks.

## Native results

Local run `reference-bench-03`, 2026-10-03: KiCad CLI 10.0.4, Freerouting 2.1.0,
Java 21 and Shapely 2.1.1. Each board has one F.Cu signal over a notched In1.Cu
ground zone with a ground through-hole pad. Requirements reserve the signal to
F.Cu and require 0.05 mm extra reference coverage beyond its 0.25 mm trace width.
No stackup, placement or design-rule change is made.

| Layers | Baseline reference result | Enhanced reference result | Baseline / enhanced copper | Baseline / enhanced seconds |
|---|---|---|---|---|
| 4 | Gap; rejected | Covered; accepted | 50.000 / 71.533 mm | 7.92 / 9.45 |
| 6 | Gap; rejected | Covered; accepted | 50.000 / 71.533 mm | 8.44 / 9.25 |
| 8 | Gap; rejected | Covered; accepted | 50.000 / 71.533 mm | 7.77 / 9.66 |

All six routes had zero vias, zero unconnected items and zero blocking DRC
findings. Three pre-existing fixture footprint warnings remain reported for
each. All source hashes were unchanged. Timings include each solver's routing
and candidate validation/refill; initial fixture preparation, source filling
and keepout projection are outside those per-solver measurements. These are
single runs, not statistical performance claims.

Regression validation: **531 tests and 234 subtests passed**, four optional
native tests skipped; the native runs above were executed separately. Ruff passed.

## What remains unresolved

- This is geometric coverage, not proof of reference-net attachment, low return
  impedance, signal integrity, EMI or manufacturing compliance. Marked copper
  islands are excluded; unmarked connectivity still requires separate analysis.
- Vias on a profiled net make otherwise covered evidence unknown because return
  transitions are not yet checked. Only explicitly adjacent reference layers,
  straight route edges and supported polygonal boundaries are accepted.
- These three cases share the same simple outer-layer route. They exercise real
  inner ground fill across different stackup sizes, but do not establish dense
  inner-layer performance or superiority over all routers. The reference test
  compares Freerouting with itself; KRT was studied, not benchmarked on this case.
- Endpoint electrical requirements, field-solver calibration, differential-pair
  coupling, delay tuning, power integrity and production UI integration remain
  separate engineering gates. Dielectric properties are never guessed.
- Keepouts can overconstrain unprofiled nets on the same layer. Per-net exclusions
  and more general board outlines need dedicated backend support and validation.

The wider hybrid-engine comparisons remain in [KRT_BENCHMARK.md](KRT_BENCHMARK.md).
