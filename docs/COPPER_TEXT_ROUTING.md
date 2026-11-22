# Copper-text-aware Vela-routing

This increment prioritizes **accepted routing quality**, not speed. A DSN that
omits text on a copper layer lets the router cross real conductive material.
Vela-routing can now add conservative keepouts derived from KiCad's actual
stroke-font rendering and validate the resulting routes against the complete
original board. This is an experimental research path, not an installed-plugin
update or a claim of superiority on every PCB.

## Implementation and boundaries

`prepare_copper_text_dsn` renders each board-level copper text item on a private,
text-only snapshot using official `kicad-cli pcb export svg`. Original board,
project, rules, schematic and settings remain unchanged. The adapter uses neither
SWIG nor a glyph-width estimate. No LLM or network endpoint receives board data.

Each visible M/L stroke is enclosed by an axis-aligned rectangle including half
its stroke width and a 0.0001 mm enclosure for SVG serialization precision. This
preserves conservative bounds for the KiCad-rendered rotations and mirroring;
the invisible searchable SVG label is ignored only when its opacity is zero.
DSN conversion flips Y once, uses the declared DSN units, preserves existing
rules, pins and keepouts, rounds box bounds outward to the declared DSN grid,
and targets only the specified copper layer. Original
clearance rules still apply. Rectangles can exclude more space than the glyphs,
so this strategy can cost route length or make a congested design infeasible.

The prepared input binds the original DSN, saved board, context and tool version.
It preserves the DSN filename because Freerouting may use that name in SES
identity. Changes invalidate the evidence. Preparation writes only a new output
directory; it does not authorize a live board write. Caller code must check
`assert_current` around routing and independently validate candidates.

Unsupported fonts/curves, visible SVG text without supported strokes, transforms
on actual SVG geometry, CSS geometry modifiers, nested SVG viewports, dynamic
text variables, footprint copper text and copper text boxes are refused. SVG
dimensions, counts, nesting, paint properties and entity declarations are
bounded/checked. The fixed KiCad SVG DTD declaration is stripped without loading
it. Silkscreen text is not converted to copper obstacles. Other omitted graphic
types remain outside this adapter's scope. KiCad DRC remains mandatory.

The benchmark also exposed a native round-trip compatibility issue: KiCad 10
adds explicit default-no via fabrication fields during save/refill. The strict
copper importer now accepts only exact default-no `capping`, `covering`,
`plugging` and `filling` fields. Enabled, duplicate, malformed or unknown settings
still fail. Via dimensions, layer span, net, UUID and trusted catalog checks are
unchanged. The Vela benchmark arm may additionally use the existing conservative
DRC-identified dangling-segment repair, followed by fresh validation.

## Reproducible authored quality evidence

Both arms use the same pinned Freerouting 2.1.0 process, Java 21, KiCad CLI 10.0.4,
placements, board stackup and original DRC rules. Solver order alternates and
failures remain in the reports. Each Freerouting call has the same 120-second
limit; Vela's optional cleanup has an additional 60-second bound. This is not
an equal-total-time comparison and makes no runtime claim. Regression tests and
another smoke run overlapped parts of the quality batch.

The authored surface-only case places a copper glyph across one signal. Both
arms are explicitly constrained to F.Cu, isolating omitted text from unrelated
inner-layer search behavior. The board configurations have 4/6/8 layers, but
these cases do **not** demonstrate dense inner-layer routing or SI sign-off.

| Case | Plain Freerouting accepted | Vela-routing accepted | Observed difference |
|---|---:|---:|---|
| 4-layer configuration, F.Cu only, 3 runs | 0/3 | 3/3 | Baseline violates copper-text clearance/short rules; Vela avoids it |
| 6-layer configuration, F.Cu only, 3 runs | 0/3 | 3/3 | Same failure mechanism |
| 8-layer configuration, F.Cu only, 3 runs | 0/3 | 3/3 | Same failure mechanism |
| 4 layers, unrestricted, one smoke run | 0/1 | 1/1 | Validated cleanup removes two dangling segments |

The F.Cu-only baseline routes were 50 mm, with zero vias, and electrically
invalid because of copper-text short/clearance violations. The clean Vela route was approximately
54.37 mm with zero vias: routing around copper costs distance. Invalid shorter
routes are not quality winners. The unrestricted smoke run recorded approximately
59.28 mm / 2 vias for the rejected baseline and 53.82 mm / 2 vias for Vela after
cleanup. This single observation does not prove broad length superiority.

All accepted candidates had zero blocking/new DRC issues and zero unconnected
items, preserved copper through native serialization, and retained three
pre-existing footprint-library warnings. All authored source hashes stayed
unchanged. The initial unrestricted 18-attempt run failed the via metadata import
gate in **both** arms; its manifest is retained separately, not presented as an
engine-routing failure or omitted from the history.

Evidence:

- [Repeated surface-only results](benchmarks/vela-copper-surface-2026-10-04.json)
- [Unrestricted smoke result](benchmarks/vela-copper-all-layers-2026-10-04.json)
- [Initial compatibility refusals](benchmarks/vela-copper-initial-refusal-2026-10-04.json)

```text
python -m velatrace.copper_text_benchmark --output <new-directory> --jar <freerouting-2.1.0.jar> --java <java-21> --kicad-cli <kicad-cli-10> --repeats 3 --surface-only
```

Omit `--surface-only` to exercise unrestricted layer search and cleanup. Results
remain local unless deliberately published. Only authored fixture manifests
are included here; private-board files and measurements are not part of this
increment's public evidence.

Regression verification: **571 tests passed, 4 skipped, 309 subtests passed**;
Ruff passed. The repeated surface-only manifest records the final adapter,
including outward DSN-grid rounding. The unrestricted smoke and initial-refusal
manifests retain their earlier implementation hashes.

## Research context and remaining work

[Official KiCad CLI documentation](https://docs.kicad.org/9.0/en/cli/cli.html)
documents layer-specific SVG export, page-size modes and single-file output.
The adapter checks the exported scale instead of assuming viewport coordinates
are physical millimeters.

[Freerouting's release notes](https://github.com/freerouting/freerouting/releases)
describe newer fanout, strict-DRC and copper-edge-clearance improvements. Those
versions have not been substituted for the pinned engine in these measurements;
these results establish no ranking against the newest upstream release or KRT.
[KiCad's DRC documentation](https://docs.kicad.org/10.0/en/pcbnew/pcbnew.html)
remains the reference for final electrical and geometric checks.

Remaining work includes more independent real designs, other omitted copper
geometry, difficult pad escape/congestion, validated impedance/differential-pair
requirements, and comparisons with newer pinned engines under matching rules.
The observed win is specific: the augmented workflow avoids a demonstrated
omitted-obstacle failure and accepts routes the current baseline rejects.
