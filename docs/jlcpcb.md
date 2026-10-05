# JLCPCB integration

Everything here is optional and off by default. VelaTrace stays offline until you click one of
the two network buttons described below. See [privacy.md](privacy.md) for the exact requests.

## Where JLCPCB parts data comes from (research, checked 2026-10-04)

JLCPCB does not publish a downloadable catalogue. Every open-source tool gets the data one of
three ways. What was found, with the numbers measured on 2026-10-04:

| Source | What it is | Size / format | Cadence | Licence | Used by VelaTrace |
|---|---|---|---|---|---|
| [Bouni/kicad-jlcpcb-tools](https://github.com/Bouni/kicad-jlcpcb-tools) parts DB on GitHub Pages (`bouni.github.io/kicad-jlcpcb-tools/`) | SQLite FTS5 (trigram) table `parts`: LCSC Part, First/Second Category, MFR.Part, Package, Solder Joint, Manufacturer, Library Type (Basic/Preferred/Extended), Description, Datasheet, Price (quantity breaks), Stock. Built by their `update_parts_database.yml` workflow from JLCPCB's public parts API ([db_build/README](https://github.com/Bouni/kicad-jlcpcb-tools/blob/main/db_build/README.md)). | "Current parts" (seen in stock within a year): a zip split into 3 chunks of at most 80 MB, **180.6 MB download, 355 MB on disk**. Variants: all parts (12 chunks, ~880 MB), Basic+Preferred only (0.35 MB). | Scheduled daily (05:00 UTC). **The published files were dated 2026-09-26 and every scheduled run from 2026-09-27 to 2026-10-04 failed**, so "daily" is the intent, not a guarantee. | Plugin code: MIT. The data files carry no licence statement; the content is JLCPCB's catalogue. | **Yes: the full catalogue (tier 1).** |
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
   over ~450 k parts with tier, stock and price breaks and needs no network afterwards. Bouni's
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
