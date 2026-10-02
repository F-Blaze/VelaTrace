# Behavior, current limits and project security

## Behavior and current limits

- **Check design** reads PCB pad/net connectivity (with pad positions) or a saved schematic/XML netlist (with pin names and electrical types) and runs the built-in checks below on this computer: no key, no provider, no network, no dialog. See [Built-in audit checks](#built-in-audit-checks).
- **Explain with AI** is optional. It needs your own provider; the privacy notice appears on first use. It sends the rule findings as context along with the design, infers functions, and waits for corrections and confirmation before classification. The project description is optional. Categories are **critical** (the design as built fails or becomes unsafe without it), **important** (quality/reliability suffers), **nice-to-have** (comfort, aesthetic or marginal benefit) and **redundant** (duplicates another function). Every verdict is a suggestion; it never removes components.
- Redundancy requires matching value, footprint/type and pin connectivity, with role and proximity checks. Sharing a rail alone is insufficient. Ambiguous cases say **possible redundancy — verify**. Code computes flags, Decimal totals, hypothetical savings and token counts; the model supplies judgments only.
- Classification displays the actual prompt's input-token count and output cap for approval. Pricing has separate consent based on the flagged count, eight-second searches and visible estimate fallback. Calls have a hard budget, output caps and at most two retries. The live usage display concerns the current response; unavailable exact streaming counts remain pending.
- `/autoroute` and `/autoroute_exit` switch the same window between modes. Session constraints stack across retries; universal constraints persist locally. Numeric constraints require confirmation. Freerouting chooses paths.
- Current routing accepts **saved, fully placed, initially unrouted boards**, straight traces, through vias, and all-net minimum width/clearance. Fresh manual DSN export is required. Existing routing, hierarchical schematic context, header keepouts and per-net constraints are refused. Incomplete routing stops; the stackup is never changed. Rejecting requires a reason.
- KiCad's IPC provides neither a dockable panel nor transient canvas overlays. VelaTrace uses an always-on-top external window and temporary `User.9` items. Panel previews are dashed violet; KiCad controls the user-layer color. Approved copper uses normal layer colors. Theme matching reads saved Windows KiCad settings when possible, with manual light/dark fallback.

The [feasibility report](feasibility.md), [UI guide](ui.md) and [routing boundaries](routing-adapters.md) describe these fallbacks and limitations.


## Built-in audit checks

Deterministic, local and conservative: when the data cannot show a problem clearly, a check stays silent. Findings are grouped as **error** (likely defect), **warning** (risky or non-standard), **saving** (cost or time can be saved) and **info**. Each has evidence, a concrete fix and, where a small passive is added or removed, an illustrative per-board cost ($0.01 per passive, not a quote). Nothing is changed automatically.

| Rule | Severity | Fires when | Known false-positive risk |
|---|---|---|---|
| `decoupling.missing` | error | An IC supply pin's net has no capacitor to ground anywhere | Without pin types (PCB source) supply pins are found by net name, so a logic pin tied to a rail counts too; it only matters if that rail has no capacitor at all |
| `decoupling.far` | warning | PCB only: the nearest such capacitor is more than 5 mm pad-to-pad from the IC's pins on that net | An unplaced board (parts parked off-board) reports distances that mean nothing yet |
| `i2c.pullup.missing` | warning | An SDA/SCL net with an IC has no resistor to a supply | Pull-ups on an off-board module or behind a connector |
| `i2c.pullup.redundant` | saving | Two or more pull-ups to the same rail on one line | A deliberately stronger combined pull-up for bus speed |
| `i2c.pullup.mixed_rails`, `i2c.pullup.zero_ohm`, `i2c.pullup.value` | warning/error | Pull-ups to different rails; a 0 Ω pull-up; a pull-up below 1 kΩ or above 100 kΩ | Very low; unparseable values are ignored |
| `led.no_resistor` | error/warning | A 2-pin LED (or series LED chain) sits across two rails with no resistor on any of its nets; warning when it hangs off an IC pin named like a GPIO | LEDs with a built-in resistor, or a constant-current driver whose pin is named like a GPIO |
| `net.single_pin` | warning (info on connectors/test points) | A designer-named net reaches only one pin | Labels reserved for later use |
| `pin.input_floating` | warning | Schematic only: an IC pin typed `input` has no connection and no no-connect flag | Inputs with internal pull-ups that the datasheet allows to float |
| `duplicate.parallel_ic` | saving (warning on I2C) | Two ICs with the same value, footprint and every pin on the same nets | Intentional parallel parts (load sharing, redundancy) |
| `connector.power_unprotected` | info | A connector carries a supply and ground with no diode, fuse, transistor or TVS/ESD part on that supply | Connectors that only supply power to another board (nets driven by a `power_out` pin are skipped) |
| `value.zero_ohm_short`, `value.missing` | error/warning | A 0 Ω resistor from a supply to ground; a passive whose value is still the symbol default (skipped when an MPN/LCSC field is set) | Very low |
| `audit.coverage` | info | No net is named like ground, so power checks could not run | None; it explains an otherwise silent result |

Heuristics: part kinds come from reference prefixes (R, C, L, FB, D, LED, U/IC, J/P/CN, F, Q, TP, Y, SW) refined by the footprint library (`Connector_*`, `LED_*`, `Resistor_*`, modules). A U part on a connector or module footprint counts as a plugged-in module, not an IC. Ground names: GND, AGND/DGND/PGND, GND*, VSS*, 0V. Supply names: anything starting with `+` (KiCad power symbols), plain voltages (3V3, 5V, 3.3V) and VCC/VDD/VBAT/VBUS/VIN/VSYS and similar, but not names such as `3V3_EN` or `VBUS_DET`. When the schematic gives pin types, `power_in`/`power_out` pins also mark supplies. Only the last hierarchical segment of a net name counts; auto-generated `Net-(…)`/`unconnected-(…)` names never match. A check that crashes becomes an info finding; the others still run.

Extra check providers (for example BOM checks) plug in with one line: `audit_rules.register_provider(bom.bom_findings)`.

## Audit and routing walkthrough (detail)

Pick the source and click **Check design**; findings appear at once. Optionally click **Explain with AI** to infer functions. Correct the cards, confirm functions, review the classification token estimate, then approve classification. Price the flagged/borderline set after its separate estimate. Missing manufacturer part numbers show **estimate only — no part number found**. Bulk and decoupling capacitors on one rail are not interchangeable duplicates.

For routing, enter `/autoroute`, edit session/universal rules using the violet constraint badge, and confirm every numeric interpretation. Click **Route board**: it checks and exports the open board itself (if KiCad cannot export automatically, export the DSN from KiCad and use **Load DSN exported from KiCad…**). Inspect the preview/validation summary before rejecting with a reason or approving. Reconfirm the full constraint list on each retry. Unsupported constraints stop the run so you can explicitly revise them.

Before every live board mutation, including preview creation/cleanup, the writer backs up the saved board, live board and saved project under `.velatrace/backups` beside the board. Malformed/unsupported SES, stale state, incomplete DRC or backup failure refuses the operation. **No save is required between User.9 preview and approval.** Validation uses the live board; later board edits require revalidation. Approval removes the owned preview and adds copper in one IPC commit, then leaves the board unsaved for normal KiCad saving. One-step Undo passed the disposable KiCad 10.0.4 test. Save project rules before the initial DSN workflow: unsaved Board Setup changes are not validated. **Do not save while temporary graphics are present.** Clear them by closing/rejecting normally. After a crash or uncertain transaction, inspect the backup/journal and KiCad state before retrying; automatic crash cleanup is not implemented. Read [write safety](write-safety.md).


## Development, verification and security

For development only, use a local environment in this checkout:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e '.[dev]'
.venv\Scripts\python.exe -m velatrace --demo
.venv\Scripts\python.exe -m ruff check src tests launch.py
.venv\Scripts\python.exe -m pytest
```

On macOS/Linux use `.venv/bin/python`. Demo data is synthetic, with no provider or IPC calls. Read-only inspection: `python -m velatrace --netlist tests/fixtures/audit/necessity.xml`.

The [2026-09-27 review](REVIEW_2026_09_27.md) records the current regression suite, complete tracked-file review and live preview-to-approval test. A disposable two-pad board passed real Freerouting, matching KiCad 10.0.4 candidate DRC, approval without saving and one-step Undo. Native tests require the paths in [testing instructions](testing.md); otherwise those two tests skip. Earlier synthetic provider calls did not establish exact token parity or complete live provider acceptance.

**F-Blaze: enable two-factor authentication on the maintainer account.** Before publishing, enforce PR-only `main` with no maintainer bypass, independent review, required CODEOWNERS/CI/CodeQL checks, secret scanning and push protection, and signed tagged releases. A solo maintainer cannot approve their own PR: `T-boy-review` has write access as the independent reviewer; add them to `.github/CODEOWNERS` so later PRs can be approved. Local policy files do not enable GitHub settings. Main protection, secret scanning and push protection are enabled. Independent PR approval is in place; release signing remains pending. See [remote setup status](REMOTE_SETUP.md) for verified settings and workflow results. See [CONTRIBUTING](../CONTRIBUTING.md) and [repository security setup](repository-security.md).

## Provider search and pricing notes

Gemini analysis works, but its web-search pricing is disabled until the required grounding presentation is implemented. Its pricing uses local illustrative estimates with zero search calls. Groq built-in search requires a supported search model chosen in Setup; search failure/timeout shows a visible reason and estimate fallback. Missing/invalid keys and outages produce explicit errors.
