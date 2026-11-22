# BOM cost checks

`velatrace.bom.bom_findings(snapshot, parts_db=None, *, boards_per_order=5)` returns `Finding`s (see `findings.py`) that save money or time on the bill of materials. It is deterministic, local and read-only: it never edits the design and never touches the network. Every finding is a suggestion with its evidence and a concrete fix.

Status: BOM tidy-ups (value and package spelling, merges) are the part with real-board evidence. JLCPCB Basic-part suggestions are untested on real data. Dollar figures are estimates, not quotes. The default parts-list source has no licence file (see below).

Without a parts list only the offline consolidation checks run. With a parts list (`velatrace.parts_db`, below) the JLCPCB Basic/Extended checks run too. Nothing needs an API key or a paid service.

## Checks

| Rule | Severity | Fires when | Guards against false positives |
|---|---|---|---|
| `bom.value_normalise` | saving | Same kind, value and package spelled differently (`100n`, `0.1uF`, `100nF`) | Refused when explicit ratings differ (`10u 6V3` vs `10u 25V`), a tolerance is stated on only some members (`10k` vs `10k 0.1%`), extra unexplained text differs, or members do not all carry the same LCSC/MPN (a generic part and a specific MPN, or two different MPNs, are different parts). Members sharing one MPN may disagree on voltage text (`1uF, 25V` / `1uF, 50V` on the same MPN): the finding says the MPN's datasheet rating is the real one. The suggested spelling never drops or lowers a stated voltage |
| `bom.value_merge` | saving | Pull-up/pull-down resistors in one package whose values are within 20% (`4.7k`, `5.1k`, `5.6k`); the target may also be any other resistor line already on the BOM (a 5.6k tach pull-up can use the board's 5.1k USB-C Rd line) | Pull roles only (see below); a value line is only removed when *every* resistor of that value is a simple pull; parts on the target line never change; the target must be an E24 value with compatible ratings; precision (<1%) parts excluded |
| `bom.package_merge` | saving | Same value in several packages (`100n` in 0402 and 0603) | Only pull and decoupling roles move; capacitors ≥1 µF (DC-bias derating), resistors <1 Ω, inductors and parts with a power rating in the value never move; voltage rating reminder for capacitors |
| `bom.jlc_basic_equivalent` | saving | An LCSC field names an Extended part and a Basic/Preferred part has the same value, package and equal-or-better ratings | Resistors and MLCCs only; dielectric never downgraded (C0G stays C0G; X7R may become X7R/X7S/X8R/C0G); voltage ≥ stated rating and ≥ rail voltage from the net name; tolerance ≤ stated tolerance |
| `bom.jlc_basic_alternative` | saving | A passive without an LCSC field has no Basic/Preferred match, but a pull resistor has a Basic value within 20% (values already on the board preferred), or a pull/decoupling part has a Basic match in another common package | Same role and rating guards; large capacitors only move to the same or a larger package |
| `bom.jlc_assign` | info | A passive without an LCSC field matches a Basic/Preferred part | Same rating guards; saves the manual search and keeps the assembler from choosing an Extended part |
| `bom.lcsc_mismatch` | warning | An LCSC field names a resistor/MLCC whose value or package differs from the schematic | Only for parts found in the parts list |
| `bom.jlc_fee_summary` | info | Summary of lines expected to be Extended and their estimated loading fee | Non-passives count only when they have an LCSC field |

Passives are recognised from the reference (`R`, `C`, `L` + number) and an imperial chip size in the footprint name (`R_0402_1005Metric` → 0402). Through-hole, electrolytic, tantalum, arrays/networks and trimmers are skipped, as are values that cannot be parsed unambiguously (a bare `100` on a capacitor, `1M` on a capacitor) and anything marked `DNP`/`DNF`/`NC`.

Value parsing accepts `4k7`, `4.7k`, `4K7`, `4700`, `4.7kΩ`, `4k7Ω`, `10 kΩ`, `4.7kohm`, `0R`, `0R1`, `2R2`, `R10`, `10u`, `100nF`, `100 nF`, `0.1µF`, `1n5`, `2M2`, European decimal commas (`4,7uF`, `2,2k`), plus rating tokens `50V`, `6V3`, `1%`, `±5%`, `X7R`, `NP0`/`C0G`, `1/4W`. Package sizes and kind words in the value (`0402 10uF`, `1k 0402 Resistor`) are ignored; the package always comes from the footprint. SI prefixes are case sensitive where it matters (`1m` = milli, `1M` = mega).

LCSC numbers are read from fields named `LCSC`, `LCSC Part #`, `LCSC#`, `JLCPCB Part #`, `JLC`, `SPN`/`SPN1`, `Supplier Part Number` and similar (case, spaces and `_-.#:/` ignored), and only when the value looks like `C<digits>`; a DigiKey number in `Supplier Part Number` is skipped. MPNs come from `MPN`, `Manufacturer Part Number`, `Manufacturer PartNo`, `Mfr. Part #`, `PartNo`, `Part Number`, `P/N` and similar.

### Roles

Roles come from connectivity only; anything not proven simple is `other` and never has its value changed.

- **Decoupling**: a two-pin capacitor between a ground net (`GND*`, `AGND`, `VSS*`, …) and a power net (`+3V3`, `3V3`, `+5V`, `VCC*`, `VDD*`, `VBUS`, `VBAT*`, `VIN`, `VSYS`, …).
- **Pull-up / pull-down**: a two-pin 1 kΩ–1 MΩ resistor with exactly one end on a power or ground net, where every other pin on the pulled net belongs to an IC, connector, switch, test point or module (`U`, `IC`, `J`, `P`, `CN`, `SW`, `S`, `TP`, `MOD`, `A`…). Another resistor or capacitor on the net (divider, RC reset/timing, filter), a diode/LED (current setting), a transistor or crystal makes it `other`. So do net or pin names that indicate a value-setting node: `FB`, `ADJ`, `RT`, `CT`, `ISET`, `PROG`, `RSET`, `SS`, `COMP`, `ILIM`, `SENSE`, `CC1`/`CC2` (USB-C Rd is spec-mandated 5.1 kΩ), `ID`, `VREF`, `ADC`/`AIN`, `CFG`/`MODE`/`SEL`/`ADDR` (analog straps), PHY/transceiver bias and reference pins (`RTX`, `RTXT`, `EXTRES`, `RREF`, `RBIAS`, `IREF`, `ISET`, e.g. a 6k04 on `EPHY_RTX`), op-amp inputs and similar.
- **Precision values**: a resistor whose value is not in the E24 series (`4.99k`, `6.04k`, `215k`: E48/E96/E192) was usually chosen for precision, so it only counts as a pull when the pulled net is named like a digital pull (`SDA`, `SCL`, `RESET`/`NRST`, `EN`, `CS`, `INT`, `IRQ`, `ALERT`, `BOOT`, `TACH`, `PGOOD`, `GPIOx`, …). An E96 value on an unnamed net is `other`.

Pin names come from schematic netlists; the IPC board reader has none, so board-only snapshots rely on topology and net names alone.

### Cost assumptions (checked 2026-09-30)

- JLCPCB Economic PCBA: Basic parts have no loading fee; **Preferred Extended** parts are exempt from the feeder-loading fee on Economic assembly; every other (**Extended**) unique part costs **$3 per order**. Source: [JLCPCB PCB Assembly FAQs](https://jlcpcb.com/help/article/pcb-assembly-faqs). Standard PCBA and other assemblers price setup differently. The constants live in `bom.py` (`JLC_EXTENDED_FEE_USD`, `JLC_FEE_CHECKED`); update both together.
- `Finding.cost_delta` is per board: the per-order fee divided by `boards_per_order` (default 5, JLCPCB's minimum PCB quantity). $3 at 5 boards is −$0.60/board. The evidence text also shows the per-order amount.
- Without a parts list, consolidation findings report the saved BOM lines/reels and leave `cost_delta` empty rather than guess whether a removed line was Extended.
- With the small parts list, part prices are not compared; passives cost fractions of a cent, so line count and setup fees dominate at prototype quantities. With the full catalogue ([jlcpcb.md](jlcpcb.md)), Basic-equivalent suggestions include the unit-price difference from the catalogue's price breaks.

## Parts list (`parts_db.py`)

**Opt-in and user-initiated.** `download_catalogue(config_dir, source=DEFAULT_SOURCE)` is the only network call and must be triggered by an explicit user action; nothing downloads on import, at startup, during an audit, or when loading. `load_catalogue(config_dir)` reads the cache offline and returns `None` if nothing was downloaded, in which case the JLCPCB checks are skipped. `clear_catalogue(config_dir)` removes it.

Cache files (the plugin config folder, i.e. the Qt `AppConfigLocation` shown in Setup): `parts-db/jlc-parts.csv` (the payload, byte for byte) and `parts-db/jlc-parts.json` (source, URL, fetch time, byte size, SHA-256, row counts).

### Source chosen

| | lrks/jlcpcb-economic-parts (default) | CDFER/jlcpcb-parts-database | yaqwsx/jlcparts |
|---|---|---|---|
| URL | `https://lrks.github.io/jlcpcb-economic-parts/economic-parts.csv` | `https://cdfer.github.io/jlcpcb-parts-database/jlcpcb-components-basic-preferred.csv` | `https://yaqwsx.github.io/jlcparts/data/` (sharded) |
| Content | JLCPCB Basic + Preferred Extended ("Economic") parts, with `deleted` flags | Basic + Preferred parts | Full in-stock catalogue, per-category shards with SHA-256 manifest |
| Size (2026-09-30) | 772,161 bytes, 2,004 rows (1,586 current: 182 Basic chip resistors, 88 Basic MLCCs) | 363,033 bytes, 452 rows | manifest alone 2.7 MB; resistor + MLCC shards ~140 KB + 346 KB attribute table |
| Cadence | weekly (last modified 2026-09-25) | daily | three times daily |
| Licence | **no licence file** in the repository | MIT | MIT |
| Status 2026-09-30 | complete, used | **refused**: 1 Basic part, 0 resistors/MLCCs (its upstream is jlcparts) | resistor/MLCC shards contain no Basic parts; lookup starts at C16000000 |

The lrks list is the smallest dataset that answers both questions (is this LCSC part Basic/Preferred? which Basic part has this value and package?) and the only one complete on the check date. Both CSV sources are supported and selectable (`source="cdfer-basic-preferred"`); a completeness check refuses any download with fewer than 100 Basic chip resistors or 40 Basic MLCCs, so a broken upstream export can never make Basic parts look Extended.

**Licensing.** VelaTrace does not ship or redistribute the data: the user downloads it directly from the publisher's GitHub Pages site into their own config folder. The data itself is JLCPCB's public part catalogue (part numbers and ratings, i.e. facts). The lrks repository has no licence file, so it grants no explicit reuse terms; the CDFER repository is MIT but is currently incomplete. Maintainers who need an explicitly licensed default should switch `DEFAULT_SOURCE` when CDFER recovers, or ask the lrks author to add a licence. Neither project is affiliated with JLCPCB, and JLCPCB may change its Basic list at any time: re-download before ordering.

**Test fixture.** `tests/fixtures/bom/economic-parts.csv` is authored for VelaTrace in the lrks column layout: 15 hand-written rows (real LCSC numbers and ratings, empty price/stock columns, plus the synthetic `C900001`). It is not a copy of either upstream file and is covered by the repository's MIT licence.

### Integrity and privacy

- One HTTPS `GET` to a fixed, allow-listed URL with verified TLS (system CA store), no cookies, no query string and no board data; the request carries only a `VelaTrace-parts-list` User-Agent. GitHub Pages sees your IP address, as with any download.
- Redirects are refused. The body must be 20 KB–8 MB and match `Content-Length`; the whole transfer has a 30-second deadline (DNS included).
- The payload must be UTF-8 CSV with the source's expected columns, at most 200,000 rows, valid `C<digits>` LCSC numbers, and pass the completeness check. Otherwise nothing is cached and the previous cache is left untouched.
- The upstream publishes no signed hash, so the SHA-256 recorded at download time guards the local cache: every load recomputes it, and a size or hash mismatch, missing file or malformed metadata is refused with a reset instruction instead of silently using suspect data.

## Limitations

- Board-only snapshots whose footprints are not library chip footprints (for example embedded footprints named just `R1`) have no package, so their passives are skipped.
- Net-name heuristics recognise common rail names; unusual rail names make parts `other` (fewer suggestions, not wrong ones). A rail named with a voltage (`+12V`) is used for capacitor voltage checks; `VCC`/`VDD` carry no voltage, so the fix text asks you to confirm the rating.
- Pull strength changes of up to 20% are assumed harmless. Confirm I²C rise time, leakage and wake-up/strap margins, especially on buses with many devices.
- Inductors, ferrite beads, electrolytics and ICs never get value or equivalence suggestions. ICs appear only in the fee summary, and only with an LCSC field.
- The fee model covers JLCPCB Economic PCBA only.
