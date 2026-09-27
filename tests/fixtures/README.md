# Test fixtures

These small, self-contained fixtures were authored for VelaTrace and are covered by the repository's MIT license. They contain no user designs or third-party library footprints.

- `audit/necessity.kicad_pcb` is a placed, unrouted two-layer board with an outline and embedded footprint geometry. `necessity.kicad_pro` supplies matching project rules; `necessity.xml` independently records the same pin-to-net connectivity.
- C1 is bulk capacitance and C2 is local decoupling on the same VCC/GND rail; they must not be flagged as duplicates.
- U1 and U2 are identical nearby temperature sensors with identical pin-to-net mappings and a confirmed shared role; U2 must be flagged as a duplicate suggestion. With only the XML netlist, missing placement downgrades this to possible redundancy.
- The audit PCB is intentionally unrouted. Unconnected items are expected; it is not a DRC-clean manufacturing reference. The test-only position join verifies XML/PCB agreement, not the production IPC reader.
- `routing/simple.dsn` is a minimal authored routing input. `routing/freerouting-2.1.0.ses` is actual external Freerouting output for that fixture. The native test runs the router again instead of relying solely on a canned SES result.

The audit PCB and project were parsed and checked by the real official KiCad 10.0.6 CLI on 2026-09-22. The regression confirms nonzero DRC/unconnected counts and refusal when a rule check is disabled. Live editor undo remains a separate release check.

Opening a fixture in KiCad and saving it may rewrite its formatting. Use a copy for interactive release checks.
