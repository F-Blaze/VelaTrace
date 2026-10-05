# JLCPCB integration

Everything here is optional and off by default. VelaTrace stays offline until you click one of
the two network buttons described below, and it never changes your design without a click, a
backup and an Undo step. See [privacy.md](privacy.md) for the exact requests.

Open it with **JLCPCB…** under the audit results (after **Check design**).

## What it does

| Feature | How | Network |
|---|---|---|
| **Parts table** | Every BOM line: references, quantity, value, package, LCSC number, tier (Basic / Preferred / Extended), stock, unit price at your quantity, Extended fee, and where the numbers came from. DNP and exclude-from-BOM parts are left out when the saved board is available. | none |
| **Catalogue** | **Download catalogue…** fetches the full JLCPCB parts database (size shown first, resumable, verified, replaced atomically) into `jlc-catalog/` in the VelaTrace config folder. Search by LCSC code, manufacturer part number or words; filter by package, tier and stock. | one explicit download from `bouni.github.io` |
| **Live stock/prices** | **Refresh JLCPCB stock/prices** asks JLCPCB about the LCSC numbers in the table, one per second, at most 200 per click; answers are cached for 6 hours in `jlc-live/` and shown as "live as of <time>". | one request per part number to `cart.jlcpcb.com` |
| **Checks** | With the catalogue installed, **Check design** also reports: an LCSC number that is not the schematic value/package (`bom.lcsc_mismatch`, now for Extended parts too), Extended parts with a Basic equivalent (`bom.jlc_basic_equivalent`, with the real unit-price difference), unassigned passives with a matching Basic part (`bom.jlc_assign`), the Extended-fee total (`bom.jlc_fee_summary`), `jlc.out_of_stock`, `jlc.low_stock` and `jlc.cost_estimate`. | none |
| **Assign** | **Assign part…** (or double-click a line) searches the catalogue and marks a part number as *pending*. **Use suggested Basic parts** fills every unassigned passive that has an in-stock Basic/Preferred match. Nothing is written yet. | none |
| **Write LCSC to board** | Writes the pending numbers into the footprints' `LCSC` field of the open PCB as one KiCad commit: backup first, one Undo step, then the live board is compared with the backup and anything other than those fields differing is reported. | none |
| **Export assignment CSV** | `Reference, Value, Footprint, LCSC` for every line with a part number (pending ones included). | none |
| **Export JLCPCB files…** | `BOM-<name>.csv` (Comment, Designator, Footprint, LCSC Part #), `CPL-<name>.csv` (Designator, Mid X, Mid Y, Layer, Rotation) and `GERBER-<name>.zip`, made by KiCad's own `kicad-cli` on a temporary copy of the live board. | none |

Without the catalogue the small Basic/Preferred list from [bom.md](bom.md) still works (tiers
and Basic suggestions only; no search, stock or prices).

### The schematic is not written

KiCad 10's plugin (IPC) API can edit footprints on the board but not the schematic: the
`kicad-python` 0.8.0 client marks its whole schematic module as "KiCad 11", and on KiCad 10.0.6
only footprint field updates were verified to work. So VelaTrace writes the PCB side and then
tells you to run **Tools > Update Schematic from PCB** with **Other fields** ticked, which
copies the `LCSC` field back to the symbols. If you prefer to edit the schematic yourself, use
the assignment CSV as the list. Until you do one of the two, a later *Update PCB from
Schematic* with "update fields" can overwrite the board-side numbers.

When a footprint already has a part-number field (`LCSC`, `JLCPCB Part`, `SPN1` holding a
`C…` number, and the other names VelaTrace reads), that field is overwritten; otherwise a
hidden `LCSC` field is added on the Fab layer. Verified on KiCad 10.0.6: the change is a single
entry in Edit > Undo.

### Fabrication files

- Made from the board as it is in the editor, unsaved changes included; KiCad runs on a
  temporary copy, so your project folder is not written. Files with the same names in the
  output folder are replaced.
- **BOM**: one line per (LCSC number, value, footprint). Parts marked *Do not populate* or
  *Exclude from bill of materials* are left out.
- **CPL**: positions from `kicad-cli pcb export pos` (millimetres, footprint anchor). Parts
  marked *Do not populate* or *Exclude from position files* are left out. Rotation follows the
  convention JLCPCB expects and kicad-jlcpcb-tools uses: bottom-side parts are mirrored
  (`180° − rotation`), then a per-package correction is added.
- **Rotation corrections**: a short built-in table for common packages (SOT-23/223/89/353/363
  +180°, SOIC/SOP/SSOP/TSSOP/MSOP/VSSOP/LQFP/TQFP/QFN/DFN +270°, polarised capacitors +180°,
  resistor arrays +90°). It is a starting point, not a guarantee: **always check JLCPCB's
  placement preview**. Add your own rules in `jlc-rotations.csv` next to the board or in the
  VelaTrace config folder, one `pattern,degrees` row each; the pattern is an LCSC number
  (`C123456`) or a regular expression matched at the start of the footprint name. Your rows win
  over the built-in ones. The community `cpl_rotations_db.csv` has the same two columns and can
  be dropped in under that name (it is GPL-3.0, so it is not bundled).
- **Gerbers and drill**: the copper layers of the board plus paste, silkscreen, mask and
  Edge.Cuts with the settings from [JLCPCB's KiCad guide](https://jlcpcb.com/help/article/how-to-generate-gerber-and-drill-files-in-kicad-8):
  Protel file extensions, soldermask subtracted from silkscreen, zones checked, no X2 or
  netlist attributes; Excellon drill files in millimetres, decimal format, absolute origin,
  alternate oval-hole mode, plated and non-plated holes in separate files. No drill map is
  generated. BOM columns follow [JLCPCB's BOM page](https://jlcpcb.com/help/article/bill-of-materials-for-pcb-assembly).

### Fee, stock and cost assumptions

- Extended loading fee: **$3.00 per unique Extended part per order** (Economic PCBA; checked
  2026-09-30, see [bom.md](bom.md)). Basic and Preferred parts have none.
- Unit prices are the part's own quantity breaks at `parts per board × boards`. Live prices are
  used when you refreshed; otherwise the catalogue's (rounded to $0.001 by its publisher and as
  old as the file).
- `jlc.cost_estimate` and the line under the table show parts plus Extended fees per board at
  5, 10, 30 and 100 boards. **Not included:** lines without an LCSC number, the PCB itself,
  assembly set-up, stencil and per-joint fees, attrition, shipping and tax. Use it to compare
  part choices, and JLCPCB's quote for the real total.
- `jlc.out_of_stock`: stock is 0, JLCPCB does not list the number, or it cannot be bought.
  `jlc.low_stock`: stock would not cover 100 boards of the design (a warning when it does not
  even cover the default 5-board order). Each finding says whether the number is live or from
  the catalogue, and its date.
- Replacement suggestions only offer Basic/Preferred parts that are in stock, with the same
  value and package and equal or better voltage, tolerance and dielectric (rules in bom.md).

## Limits

- **Not an official JLCPCB product or API.** The catalogue is a community build and the live
  lookup is the website's own undocumented endpoint. Either can change or disappear; the
  offline features keep working and errors are shown, never hidden.
- The catalogue is a snapshot. On 2026-10-04 the newest published build was eight days old
  because the upstream job was failing. Refresh live before ordering.
- JLCPCB does not expose a lifecycle status in either source, so "not recommended for new
  designs" is **not** detected; only stock is.
- The live endpoint does not distinguish Preferred from Extended; the tier then comes from the
  catalogue.
- No schematic write-back (see above), no per-part position offsets in the CPL (positions are
  footprint anchors, not pad centres), no design variants, no flex-PCB layers, no in-app
  datasheet or part-image viewer, no symbol/footprint download.
- The catalogue needs about 0.8 GB of disk and Python's SQLite with FTS5 trigram support
  (SQLite 3.34+; KiCad 10's bundled Python has 3.51).

## Compared with kicad-jlcpcb-tools

[kicad-jlcpcb-tools](https://github.com/Bouni/kicad-jlcpcb-tools) is the established plugin
for this job and VelaTrace uses the parts database its authors publish. Compared against its
`main` branch and README on 2026-10-04.

| | kicad-jlcpcb-tools | VelaTrace |
|---|---|---|
| Parts database | Same data; choice of all / current / Basic+Preferred / empty | Same data, "current parts" only (plus the small CSV list) |
| Download | Chunked, resumable, SQLite `quick_check` | Chunked, resumable, size shown first, zip CRC, `quick_check`, completeness floor, own SHA-256 |
| Search | Parametric search dialog, sortable columns, categories, part details with image and datasheet | Text/MPN/code search with package, tier, stock and exact-value filters; no image or datasheet viewer |
| Assign LCSC numbers | Board fields **and automatic saving into the schematic files**, remembered footprint/value mappings, design variants | Board fields in one backed-up, verified Undo step; schematic via KiCad's *Update Schematic from PCB*; no remembered mappings |
| BOM / CPL / Gerber | Yes, plus position offsets, a corrections manager, per-variant outputs and flex layers | Yes; rotation corrections from a built-in table and a CSV file; no offsets |
| Stock | Flags parts whose catalogue stock is unknown or too low for the board count | Out-of-stock and low-stock findings from the catalogue, or live for the whole BOM in one click, cached and time-stamped |
| Cost | BOM estimator with assembly pricing (uses live lookups for some part metadata) | Parts at their price breaks plus Extended fees at 5/10/30/100 boards; no assembly, stencil or PCB cost |
| Part-number checks | — | LCSC number that contradicts the schematic value/package; Extended parts with an in-stock Basic equivalent, with fee and unit-price difference; next to the electrical audit |
| KiCad API | SWIG `pcbnew` plugin inside the PCB editor | IPC API plugin in its own process |
| Network | Database, corrections file and estimator lookups as part of normal use | Nothing is contacted until a button is clicked and confirmed; hosts and payloads are documented |

In short: kicad-jlcpcb-tools is ahead on search depth, schematic write-back, variants,
corrections management and the breadth of its cost estimate; VelaTrace is ahead on checking
the BOM for wrong part numbers and cheaper Basic equivalents, on live stock for the whole BOM,
and on write safety and network transparency.

## Where JLCPCB parts data comes from (research, checked 2026-10-04)

JLCPCB does not publish a downloadable catalogue. Every open-source tool gets the data one of
three ways. What was found, with the numbers measured on 2026-10-04:

| Source | What it is | Size / format | Cadence | Licence | Used by VelaTrace |
|---|---|---|---|---|---|
| [Bouni/kicad-jlcpcb-tools](https://github.com/Bouni/kicad-jlcpcb-tools) parts DB on GitHub Pages (`bouni.github.io/kicad-jlcpcb-tools/`) | SQLite FTS5 (trigram) table `parts`: LCSC Part, First/Second Category, MFR.Part, Package, Solder Joint, Manufacturer, Library Type (Basic/Preferred/Extended), Description, Datasheet, Price (quantity breaks), Stock. Built by their `update_parts_database.yml` workflow from JLCPCB's public parts API ([db_build/README](https://github.com/Bouni/kicad-jlcpcb-tools/blob/main/db_build/README.md)). | "Current parts" (seen in stock within a year): a zip split into 3 chunks of at most 80 MB, **180.6 MB download, 804 MB on disk, 797,477 parts**. Variants: all parts (12 chunks, ~880 MB), Basic+Preferred only (0.35 MB). | Scheduled daily (05:00 UTC). **The published files were dated 2026-09-26 and every scheduled run from 2026-09-27 to 2026-10-04 failed**, so "daily" is the intent, not a guarantee. | Plugin code: MIT. The data files carry no licence statement; the content is JLCPCB's catalogue. | **Yes: the full catalogue (tier 1).** |
| [yaqwsx/jlcparts](https://github.com/yaqwsx/jlcparts) | The upstream pipeline most others derive from. Its workflow runs three times a day and now reads JLCPCB's and LCSC's **official keyed APIs** (repository secrets `JLCPCB_APP_ID`, `JLCPCB_ACCESS_KEY`, `LCSC_KEY`). Publishes `cache.sqlite3` as a multi-volume zip (`cache.z01`… + `cache.zip`, 50 MB volumes). | Multi-volume zip: Python's `zipfile` cannot read it; needs 7-Zip. | 3×/day | MIT (code); data unlicensed. | No: needs an extra tool to unpack. |
| [CDFER/jlcpcb-parts-database](https://github.com/CDFER/jlcpcb-parts-database) | Daily re-pack of yaqwsx's data: `jlcpcb-components.sqlite3` (plain SQLite + FTS5) and a Basic/Preferred CSV. | README says ~1 GB; **on 2026-10-04 the file was 53 MB with 54,843 parts and one Basic part** (upstream re-seed in progress). | Daily 06:00 UTC | MIT (code); data unlicensed. | CSV: existing optional small list. SQLite: no, incomplete on the day checked. |
| [lrks/jlcpcb-economic-parts](https://github.com/lrks/jlcpcb-economic-parts) | CSV of Basic + Preferred Extended parts (0.77 MB). | CSV | Weekly | No licence file. | Yes: the existing small list, kept as fallback. |
| JLCPCB official API ([api.jlcpcb.com](https://api.jlcpcb.com/)) | Components API with real-time price/stock. | — | live | Requires a developer account, an application and approval, then signed requests with your keys. | **No**: a mandatory account and key breaks "free, no account". |
| JLCPCB web endpoint `cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode=C…` | The unauthenticated JSON the JLCPCB parts page itself loads; kicad-jlcpcb-tools uses it for its part-details window ([lcsc_api.py](https://github.com/Bouni/kicad-jlcpcb-tools/blob/main/lcsc_api.py)). Returns assembly stock, price breaks, Basic/Extended. | ~6.5 kB JSON per part | live | Undocumented, no stability promise. Not disallowed by `robots.txt` (which blocks `/api`, not this path). No login, no key. | **Yes: optional live refresh (tier 2)**, one request per part you ask about. |
| LCSC web endpoint `wmsc.lcsc.com/ftps/wm/product/detail` | LCSC shop detail JSON. | ~36 kB | live | Undocumented. | No: it reports LCSC shop stock, not JLCPCB assembly stock (it showed 0 for a part with 29 M in JLCPCB's assembly stock). |
| EasyEDA `easyeda.com/api/products/<code>/components` (used by [easyeda2kicad](https://github.com/uPesy/easyeda2kicad.py), AGPL-3.0) | Symbols/footprints/3D models, not stock or price. | — | live | Undocumented. | No: out of scope. |
| [matthewlai/JLCKicadTools](https://github.com/matthewlai/JLCKicadTools) `cpl_rotations_db.csv` | Community rotation corrections; kicad-jlcpcb-tools downloads it. | CSV | occasional | **GPL-3.0**, so it is not copied into this MIT project. | No. VelaTrace ships its own short table and reads a file in the same two-column format if you add one. |

### Why two tiers

1. **Catalogue, downloaded on demand, searched offline.** One explicit download gives search
   over 797,477 parts with tier, stock and price breaks and needs no network afterwards. Bouni's
   "current parts" file is the only complete, stdlib-readable (zip + SQLite) public catalogue
   found, and it is the same data the competing plugin uses, so results are comparable. It is a
   snapshot: stock in it is as old as the file.
2. **Live refresh for the parts on your BOM only.** Stock is what actually blocks an assembly
   order and a snapshot cannot answer it. The JLCPCB part-detail endpoint answers it per part
   without an account. It is undocumented, so VelaTrace uses it only when you click, only for
   the part numbers on your BOM, at most one request per second, cached, and everything still
   works when it is unreachable.

Integrity: none of these sources publishes a signature or hash. VelaTrace checks what can be
checked: verified TLS to a fixed host, exact `Content-Length` per chunk, the zip CRC-32,
SQLite `quick_check`, the expected columns, and a plausibility floor (a catalogue with too few
Basic resistors/capacitors is refused, as on 2026-10-04 with the CDFER file). It then records
its own SHA-256 so later local corruption is detected.
