# Vela-routing

Vela-routing is the name of VelaTrace's experimental routing system. It combines
external routing engines with explicit constraints, independent checks and
measured candidate selection. Freerouting and KiCadRoutingTools retain their own
names, versions and licenses. Since 2026-10-12 it also has a native engine of
its own, described next. Since 2026-10-15 that native engine is the router behind
the plugin's **Route board** (`vela_router.py`, no Freerouting fallback); every
route still passes the same preview, KiCad DRC and approval steps. Not yet released.

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
  only if not worse. Dropped connections then go through a rip-up queue: the net
  is routed through whatever blocks it, the crossed nets give way and re-route
  only their crossed paths, and any that no longer fit take their turn. The best
  state seen is kept. Apart from the first full rounds and the tidy-up, only the
  paths in conflict are re-routed, not whole nets.
- **Neck-down.** A class width is treated as a default: a pad the class width
  cannot leave gets a short escape at the board minimum (0.15 mm floor), reported
  as `necked_mm`.
- **Restarts.** Up to three re-route orders are tried inside the time budget and
  the best is kept. The order is seeded; run-to-run repeatability was not measured.

### 4+ layers and electrical behaviour

- **Reference planes.** An inner layer that is at least 30 % copper zone is a
  plane. Surface pads of the plane's net drop their own via into it (or join
  copper within about 1 mm that already does); through-hole pads inside the plane
  count as connected.
- **One plane stays solid, the other may carry signals.** The hand-routed
  originals of all four 4-layer test boards have signal tracks on an inner plane
  layer. The engine keeps the plane next to the busier component side unbroken as
  the reference. A second plane layer also carries signals (at 1.5x cost), but
  only when every net of that plane also owns the solid one, so a signal can never
  cut a pad off from its plane. Listed as `mixed_planes`; the length is reported
  as `signal_on_plane_mm`. As a last resort a dropped connection may use any plane
  layer.
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
- **Pairs.** Nets named like `USB_D+`/`USB_D-` (also KiCad's automatic
  `Net-(U3-USB_DP)` names) are routed as a coupled pair where possible and
  delay-matched afterwards; see "Differential pairs and delay matching" below.

All figures are quasi-static estimates (the existing Hammerstad-Jensen
microstrip and the IPC-2141 stripline approximation). They are not a field
solver and not signal-integrity sign-off.

### Measured on the 13-board set

Same stripped open-source boards as the 2026-10-04 comparison. Accepted means
what the plugin means: KiCad DRC with refilled zones reports nothing unconnected
and no error or new warning. One run per board, three or four boards running at
once on one PC, 400 s budget. "Before" is the first version of the engine
(2026-10-12, PR #41 as opened); "after" adds mixed plane layers, the rip-up
queue, partial re-routing and squared-up joins (2026-10-13).

| Board (layers) | Before: connected | After: connected | After: new DRC issues | Accepted before / after | Time before / after |
|---|---|---|---|---|---|
| practice (2) | 8/8 | 8/8 | 0 | yes / yes | 5 s / 1 s |
| Prod_v2 (2) | 15/15 | 15/15 | 0 | yes / yes | 14 s / 7 s |
| tp4056-power-path (2) | 8/8 | 8/8 | 0 | yes / yes | 1 s / 1 s |
| anvil-stm32f031 (2) | 117/117 | 117/117 | 0 | yes / yes | 106 s / 70 s |
| esp32-4ch-relays (2) | 154/154 | 154/154 | 0 | yes / yes | 96 s / 42 s |
| mtb-led-atmega328pb (2) | 222/222 | 222/222 | 1 (starved thermal) | yes / **no** | 221 s / 198 s |
| usb-i2c-bridge (2) | 57/57 | 57/57 | 0 | no / no: 4 starved-thermal errors already on the unrouted board | 17 s / 5 s |
| bms-buck-boost (2) | 88/89 | 88/89 | 0 | no / no: also 109 errors already on the unrouted board | 201 s / 76 s |
| zereader-pico (2) | 221/223 | 223/223 | 4 (2 starved thermal, 2 track not centred on via) | no / no | 318 s / 270 s |
| rp2040-breakout (4) | 107/108 | **108/108** | 0 | no / **yes** | 183 s / 50 s |
| soleil (4) | 176/179 | 176/179 | 1 (connection-width warning) | no / no | 304 s / 365 s |
| mikoto (4) | 161/166 | 162/166 | 2 (vias already on the board left dangling) | no / no | 209 s / 401 s |
| bitaxe-ultra (4) | 280/289 | 285/289 | 1 (unidentified) | no / no | 414 s / 404 s |
| **All 13** | **1614/1635 (98.7 %)** | **1623/1635 (99.3 %)** | | **6 / 6** | **2089 s / 1890 s** |

Open connections fell from 21 to 12; fully connected boards rose from 7 to 9.
The first 4-layer board (rp2040-breakout) is now routed completely and accepted
by KiCad DRC, with In1 kept solid and about 92 mm of signal on In2. The other
three 4-layer boards are at 97.6 to 98.6 %; their second plane belongs to a
power net with no other plane, so it stays solid. mtb-led lost its acceptance to
one new starved-thermal error: tracks that pass close to a pad can cut the
spokes that tie it to a copper pour, and the engine does not model that. A soft
keep-off ring around such pads was tried and removed (slower, no fewer errors).
Results still vary with the re-route order.

Also tried and dropped: a finer grid (tracks pack 14 % tighter, 7x slower, no
more connections on rp2040).

For context only, the record of 2026-10-04 for the Freerouting path on the same
boards (900 s cap, not re-run, so not a side-by-side test): accepted practice;
85.7 % on usb-i2c-bridge; no plan within the cap on anvil, bms,
esp32-4ch-relays and rp2040-breakout; refused tp4056, bitaxe, zereader and
mtb-led at import; mikoto and soleil not measured. Prod_v2 was rejected then;
the smoke test on main now expects it accepted. This is 13 boards, one run each,
on one PC. It supports "routes more of this set", not a general ranking. A
same-day Freerouting run is still to do.

### Third round and a same-day Freerouting run, 2026-10-14

Added since the table above: any second plane layer may carry signals, with the
plane's continuity checked on the grid afterwards and cut-off pads re-joined
(`plane_connections_mended`); net-tie footprints; a dropped signal may be drawn
at the escape width end to end; plane drops search the whole board and get a
second pass; vias already on the board are linked to their net.

Both routers ran on the same 13 boards on the same PC with a 500 s cap. The
Freerouting column is the path the plugin uses today (current `main`, through
the bench harness `cmp_run.py` arm A), judged by the plugin's own validator. One
run each, so order-to-order variation is not averaged out. The two routers did
not run at the same moment; each shared the PC with two or three other boards.

| Board (layers) | Vela: connected | Vela: new DRC issues | Vela accepted | Freerouting: left unconnected | Freerouting accepted |
|---|---|---|---|---|---|
| practice (2) | 8/8 | 0 | yes | 0 | yes |
| Prod_v2 (2) | 15/15 | 0 | yes | 0 | yes |
| tp4056-power-path (2) | 8/8 | 0 | yes | refused at import (layer check) | no |
| anvil-stm32f031 (2) | 117/117 | 0 | yes | 14, plus 4 new DRC issues | no |
| esp32-4ch-relays (2) | 154/154 | 0 | yes | 1 | no |
| mtb-led-atmega328pb (2) | 222/222 | 1 (starved thermal) | no | 7, plus 3 warnings | no |
| usb-i2c-bridge (2) | 57/57 | 0 (4 errors already on the board) | no | 2, plus 3 new DRC issues | no |
| bms-buck-boost (2) | 88/89 | 0 (109 errors already on the board) | no | 20 | no |
| zereader-pico (2) | 223/223 | 4 | no | result refused (placement list changed) | no |
| rp2040-breakout (4) | 108/108 | 0 | yes | 3, plus 1 warning | no |
| mikoto (4) | 166/166 | 1 (a via already on the board) | no | not measured (harness board load timed out) | - |
| soleil (4) | 176/179 | 1 | no | not measured (harness board load timed out) | - |
| bitaxe-ultra (4) | 282/289 | 8 | no | result refused (placement list changed) | no |
| **Total** | **1624/1635 (99.3 %)** | | **6 of 13** | | **2 of 11 measured** |

Vela connects every pad on 10 of the 13 boards; Freerouting on 2 of the 11 it
was measured on. Four of Freerouting's misses are not routing failures: one
import refusal, two results the plugin's validator refused, and two boards the
harness could not load. On the seven boards where both produced a checked
route, Vela left 1 connection open in total and Freerouting 47. Freerouting was
faster where it finished (10 to 160 s; Vela 2 to 220 s on the same boards).

Still open on the 4-layer boards: soleil (a centre ball of a 0.5 mm BGA whose
only escape clears by 6 um, less than the grid margin; a 0.5 mm power net with
no narrower fallback; one signal) and bitaxe (the run hit the time cap after
seven negotiation rounds, each 50 to 110 s). mikoto is fully connected but keeps
one DRC error on a via that was already on the stripped board. Starved-thermal
errors still cost mtb-led and zereader their acceptance.

### Speed-up and a three-order comparison, 2026-10-14 (later)

Changes: the A* search now expands each cell once (closed set). Before, the
weighted heuristic re-expanded congested windows many times over. Cost windows
are passed to the search without copying, and the A* weight is 1.2. Two changes
from the round above were measured and reverted: spoke corridors around
through-hole pads (more starved thermals) and a wider fallback for power nets
(soleil lost connections). Total time on the 13 boards fell from 2208 s to
958 s for one order each.

Freerouting is not re-run for each Vela change. Its result is frozen in
`VelaTrace-bench/runs/freerouting-baseline-2026-10-14/`: 3 runs per board (one
for mikoto, soleil and tp4056), main 5ceaa61, 500 s cap. Vela ran each board
with three different re-route orders (seeds 1, 4 and 7), 600 s budget, judged
by KiCad DRC.

| Board (layers) | Vela accepted | Vela left unconnected (min/med/max) | Vela time (median) | Freerouting accepted | Freerouting left unconnected (median) | Freerouting time (median) |
|---|---|---|---|---|---|---|
| practice (2) | 3/3 | 0/0/0 | 2 s | 3/3 | 0 | 6 s |
| Prod_v2 (2) | 3/3 | 0/0/0 | 5 s | 3/3 | 0 | 8 s |
| tp4056-power-path (2) | 3/3 | 0/0/0 | 0 s | 0/1 | refused at import | - |
| esp32-4ch-relays (2) | 3/3 | 0/0/0 | 31 s | 1/3 | 0 | 79 s |
| mtb-led-atmega328pb (2) | 2/3 | 0/0/0 | 57 s | 1/3 | 0 | 98 s |
| anvil-stm32f031 (2) | 2/3 | 0/0/0 | 39 s | 0/3 | 16 | 160 s |
| usb-i2c-bridge (2) | 0/3 | 0/0/0 | 22 s | 0/3 | 5 | 74 s |
| bms-buck-boost (2) | 0/3 | 0/0/1 | 41 s | 0/3 | 15 | 149 s |
| zereader-pico (2) | 0/3 | 0/0/0 | 75 s | 0/3 | result refused by validator | 299 s |
| rp2040-breakout (4) | 3/3 | 0/0/0 | 34 s | 0/3 | 5 | 130 s |
| mikoto (4) | 0/3 | 1/3/3 | 29 s | 0/1 | result refused by validator | 238 s |
| soleil (4) | 0/3 | 1/2/2 | 196 s | 0/1 | result refused by validator | 279 s |
| bitaxe-ultra (4) | 0/3 | 2/3/4 | 607 s | 0/3 | result refused by validator | 445 s |
| **Accepted runs** | **19 of 39** | | | **8 of 33** | | |

Vela is accepted on 8 boards in at least one order, Freerouting on 4. Vela
connects every pad in every order on 9 boards and is faster on every board both
finish. Where neither is accepted, Vela's misses are small: pre-existing errors
(usb-i2c, bms), a few new DRC items (zereader: teardrop in a keep-out, starved
thermal), or 1 to 4 open connections on three 4-layer boards. Five Freerouting
misses are refusals by the plugin pipeline on main (layer check at import, SES
placement check), not routing results.

Still open for Vela: mikoto's plane-fill estimate lets a split power plane on
In2 be cut (KiCad finds 1 to 3 vias cut off); soleil (one 0.5 mm BGA ball the
grid cannot reach, one power net); bitaxe needs the full 600 s and ends with 2
to 4 open; starved-thermal errors on a few 2-layer boards.

### Plane keep-outs and thermal reliefs, 2026-10-15

- Rule areas that forbid copper pour (`copperpour not_allowed`) are cut from
  every pour before plane detection. Before, vias could land where KiCad fills
  no copper (mikoto: 3/1/3 vias cut off, now 0 on all three orders).
- Thermal reliefs: the loader reads each pour's connect mode, thermal gap and
  spoke width and each pad's zone connection and spoke angle. After routing,
  nets whose tracks leave a pad with only one live spoke (KiCad's
  "starved thermal") are re-routed around two reserved spoke corridors; the
  change is kept only if every net still routes cleanly. The threshold for a
  spoke counting as cut was fitted against KiCad's fills on these boards.

Same comparison as above (Vela seeds 1, 4, 7; Freerouting frozen baseline):

| Board (layers) | Vela accepted | Vela left unconnected (min/med/max) | What still blocks Vela | Freerouting accepted |
|---|---|---|---|---|
| practice, Prod_v2, tp4056 (2) | 3/3 each | 0 | - | 3/3, 3/3, 0/1 |
| esp32-4ch-relays (2) | 3/3 | 0 | - | 1/3 |
| mtb-led-atmega328pb (2) | 3/3 | 0 | - | 1/3 |
| anvil-stm32f031 (2) | 2/3 | 0 | one dangling-track warning on seed 7 | 0/3 |
| rp2040-breakout (4) | 3/3 | 0 | - | 0/3 |
| mikoto (4) | 0/3 | 0/0/0 | only errors already on the unrouted board | 0/1 (refused by validator) |
| usb-i2c-bridge (2) | 0/3 | 0/0/0 | only errors already on the unrouted board | 0/3 |
| bms-buck-boost (2) | 0/3 | 0/0/1 | errors already on the unrouted board | 0/3 |
| zereader-pico (2) | 0/3 | 0/0/0 | a teardrop in a keep-out; a connection-width warning | 0/3 (refused by validator) |
| soleil (4) | 0/3 | 1/2/2 | 1-2 open, connection-width warnings | 0/1 (refused by validator) |
| bitaxe-ultra (4) | 0/3 | 2/3/4 | hits the 600 s budget with six boards running at once (routed completely when it ran with less load) | 0/3 (refused by validator) |
| **Accepted runs** | **20 of 39** | | | **8 of 33** |

On 11 of 13 boards Vela connects every pad in every order and adds no DRC
error beyond warnings; on mikoto, usb-i2c and bms only errors that were already
on the unrouted board stop acceptance.

### Finishing dense boards: ideas from KiCadRoutingTools, 2026-10-16

Two completion mechanisms re-implemented in Vela's own design from the ideas of
KiCadRoutingTools 0.23.1 (ideas from KiCadRoutingTools, MIT, (c) 2026 drandyhaas).
No KRT code is used, and unlike KRT nothing rewrites the board's design rules:
KiCad DRC stays the only judge.

- **KiCad check and reconnect** (`kicad_check`, `Router.reconnect`). With a
  kicad-cli (the runner's `--kicad-cli`, or the plugin's KiCad), the routed copy
  goes through `kicad-cli pcb drc --refill-zones`. Each pair KiCad reports open
  (`DrcResult.missing`: item descriptions with positions) is mapped back to the
  net's copper: a pad by its position, a track or via by the nearest copper Vela
  drew, a zone by the rest of the net. Vela then tries, in order: a straight
  off-grid track between two pads of the net within 2 mm, checked exactly
  against all other copper (fine-pitch neighbours the grid phase walls off; if
  only routed copper is in the way, one rip-up turn moves it); a strict search
  between the two copper islands; one rip-up turn of the rescue. KiCad checks
  again, at most two rounds. A round KiCad counts no better (open connections
  plus violations) is rolled back. `--kicad-rounds 0` only judges. Time in
  KiCad is reported as `kicad_check_s` and left out of `seconds`. A new restart
  is only begun if the last one fits in what is left plus 30 s, so the check
  has time (a cut-short restart never won on the bench).
- **Rip-up churn control** (`Router._turn`, `rescue`). A net that loses a
  connection as a rip victim costs more to push through each later turn (a
  per-net surcharge in the search window; none while it is open anyway).
  Victims re-route least ripped first. A net ripped three times waits for a
  final round instead of trading places (soleil seed 1 spent all its turns
  swapping +BATT and U7-STAT2). A turn that leaves more open than it found, or
  a conflict, is undone; the best state seen is kept as before.

Measured on the 13 boards, seeds 1, 4, 7, 600 s, three boards at a time
(runs c1/c4/c7 against f1/f4/f7). The machine was busier than for the
reference runs, so times are noisy; open connections are KiCad's count.

| Board | Open before (1/4/7) | Open after | Router s after (before) |
|---|---|---|---|
| soleil | 2/1/2 | 1/1/1 | 264/214/247 (229/188/212) |
| bitaxe-ultra | 4/3/2 | 4/4/3 | 580/607/513 (613/627/601) |
| other 11 boards | unchanged | unchanged | mostly lower; mtb-led and zereader 20-50 % higher in this run only |

- soleil: the KiCad check closed one connection on seed 1 (V_CHARGE, by a
  rip-up turn). What stays open is GND ball B2 in the middle of the 0.5 mm
  DSBGA U9: the only way out is a 0.15 mm track to ball C1 between B1 and C2
  with 6 um to spare. U9 sits 0.7 cell off the grid phase, and B1's 0.3 mm
  track end swells past its pad; moving it in a rip-up turn leaves B1 and C2
  unreachable, so the link is refused.
- bitaxe: the check closed one connection on seed 1 (5 to 4 open). Two runs of
  the final code gave 4/4/3; an earlier full run of nearly the same code gave
  4/1/2. The difference to the reference is within run-to-run noise. Routing
  now ends inside the budget.
- Same connections, KiCad-unconnected, new DRC and acceptance on every other
  board and seed (20 of 39 runs accepted, as before).
- A third idea, a search budget extended in tranches only while the search
  closes in on its goal, was tried and dropped: failing searches were already
  cheap (the backwards probe finds walled-in pads), and soleil gave identical
  results and times with and without it.

### Differential pairs and delay matching, 2026-10-18

Ideas from KiCadRoutingTools 0.23.1 (MIT, (c) 2026 drandyhaas): a pair routed as
one centreline and offset to its two tracks (`diff_pair_routing.py`), and
trombone meanders on the faster net matched in time per layer
(`length_matching.py`, `impedance.py`). Re-implemented in Vela's own design; no
KRT code is used and no design rule is loosened.

- **Trunk.** Width `w` and gap `g` come from the net class's `diff_pair_width` /
  `diff_pair_gap` (.kicad_pro; class width and clearance when unset; the gap is
  never below clearance + 0.02 mm, the grid's keep-off margin). The pair's ends
  are the two places farthest apart where a P and an N pad sit within 3 mm. A
  one-layer A* on a virtual class `2w + g` (an extra occupancy map, open ground
  cheaper than ground next to pins) joins free cells within a launch radius
  (0.75 to 4.5 mm) of each end, the pads' own layer first. The centreline is
  straightened, offset to +-(w + g)/2 with mitred 45/90-degree corners and each
  track checked exactly against all fixed copper.
- **Breakouts.** Each track becomes fixed copper of its net (a pad-like trunk,
  `Router._add_trunk`); the nets' own searches then only join pads to it
  (Kruskal takes trunk joins first). A candidate trunk is kept only if both nets,
  alone on the board, route all their pads through it. After routing, a trunk
  that left its pair unrouted or that the routes bypass gives way, and
  `route_board` re-routes that order without it; trunk ends past the outermost
  joins are cut (KiCad would report them dangling) and own vias stay off the
  trunk (a track end inside a via must sit on its centre).
- **Delay matching** (`Router.tune`, end of `run`). The faster net of each pair
  (delay from the stackup model, per segment layer and width, plus via barrels)
  gets exact bumps on the outer side of its trunk, or, uncoupled, square grid
  bumps on its straight runs in cells free of all other copper (residue at most
  one grid step's delay, about 0.6 ps at 0.1 mm), until skew <= `Options.skew_ps`
  (1 ps). Bump height is capped by `Options.meander_mm` (1 mm).
- **Report.** `electrical.pairs`: coupled, layer, width, achieved gap, coupled
  length and % of the routed length, skew in ps and mm before and after tuning,
  meander count, and the reason when a pair routes as two nets.

### Custom rules and BGA escape, 2026-10-19

Ideas from KiCadRoutingTools 0.23.1 (MIT, (c) 2026 drandyhaas): reading the
routing subset of `.kicad_dru` in KiCad's order and listing what is not modelled
(`kicad_dru.py`, `design_rules.py`), and under-pad escape of dense ball arrays
(`bga_fanout/underpad.py`). Re-implemented in Vela's own design; no KRT code is
used.

- **Custom rules** (`vela_board._custom_rules`). Constraints `clearance`,
  `track_width`, `via_diameter`, `hole_size`, `hole_clearance`, `edge_clearance`
  and `disallow` (track, via, through_via). Conditions: `NetClass`, `NetName`
  (case-blind, `*`/`?` wildcards), `hasNetclass()`, `Type`, `Pad_Type`,
  `Via_Type`, `&&`, `||`, `!`, A/B, the `(layer ...)` clause and
  `intersectsArea`/`insideArea`/`enclosedByArea` on named rule areas. As in
  KiCad the last matching rule wins; a pad or footprint clearance still applies
  on top. A condition is evaluated per net as true, false or "depends" (on the
  layer, an area or the other item B); a "depends" rule is applied net-wide on
  the safe side (higher clearance, width clamped to its min/max) and noted.
  Clearances are only ever raised, because VelaTrace also proves a route under
  Board Setup rules alone. `disallow` becomes a keep-out over the named rule
  areas (or the whole board). A per-net minimum width turns off that net's
  neck-down. Every rule, constraint or condition not modelled (`assertion`,
  `isPlated()`, net-specific `disallow`, ...) is listed in report.json `notes`.
  On the bench only soleil and zereader-pico carry a `.kicad_dru` (AISLER's
  rules); neither changes routing (PTH hole size, no blind/micro vias, a
  solder-mask `assertion`, which is noted).
- **BGA escape** (`Router._escape`, before routing). Footprints whose SMD pads
  sit on a square pitch, at least 3x3 and half filled, are ball arrays. An inner
  ball is walled in when the grid, flooding its free cells from the ball, gets
  neither past the outer balls nor to a cell where a via of its net fits. Such a
  ball gets fixed copper (a pad-like trunk, as for pairs), each piece checked
  exactly: a straight track to a same-net ball that is not walled in, else a via
  in the pad, else a dog-bone via on a diagonal between four balls. soleil's
  0.5 mm DSBGA centre ball (GND) clears its neighbours by 6 um with a 0.15 mm
  track to the GND corner ball, less than the grid's keep-off margin; it now
  escapes that way.

Measured with seeds 1, 4 and 7 on all 13 boards, budget 600 s, against the step-2
engine (runs d1/d4/d7; d1 where a seed was not run). Only soleil changed
because of this step: 1 open connection on every seed before, 0 after, and one
`connection width` warning fewer (blocking 16/16/15; all other blockers are
courtyard and silk errors already on the unrouted board, so soleil is still not
accepted). Totals over 39 runs: accepted 21 vs 21, unconnected 9 vs 20,
blocking 406 vs 407. The rest of the unconnected drop is bitaxe-ultra (8 vs 17),
which an A-only run with identical inputs also reached (12 in total): it comes
from the baseline having been run on a busier machine (more re-route tries fit
the budget now), as do the shorter times (2603 s vs 4913 s in total; A-only
2872 s). bms seed 7 leaves 1 open with the step-2 engine too. Smoke (demo,
practice, Prod_v2): all accepted.

Step 4 (measured 2026-10-19, 13 boards x seeds 1/4/7, 600 s, same harness):
tracks leaving a teardrop pad keep a teardrop half width off no-track keep-outs
(KiCad checks the teardrop, not the track), and thermal relief counts spokes a
fixed pair trunk cuts as cut. zereader-pico: "items not allowed" gone on all
seeds; blocking 4/2/3 -> 4/1/1. Its remaining starved thermals are U2 pad 9
(already on the unrouted board) and J3 SH2 on seed 1, where routing fences the
spokes' fill into an island (not fixed: a gate/pocket model was tried and
reverted, its fill estimate gave false pockets). Totals: accepted 21 vs 21,
unconnected 10 vs 9 (soleil seed 1: the KiCad reconnect round was rolled back),
blocking 401 vs 406, 2539 s vs 2603 s. bitaxe-ultra stays at 2-3 open: its
BM1366 QFN pins are walled in by neighbours' jogs; reserved escape lanes were
tried (4/4 open, regressions elsewhere) and reverted.

### Fine-pitch via fanout, 2026-10-22

bitaxe-ultra's BM1366 (U14) sits on B.Cu with 2 x 14 pins at 0.5 mm pitch, and
every signal it carries ends on F.Cu, so each pin needs a via. 0.8 mm vias
cannot sit side by side at that pitch. Placed one net at a time, the late nets
found every via spot taken. Two stub repairs (on a restart, then inside the
KiCad reconnect loop) joined their pins but left the displaced neighbours
unroutable; both were dropped. The fix plans the vias together before routing,
the way a designer fans out such a part (lane-planning idea from
KiCadRoutingTools' fanout, no code). It applies to straight rows of 5 or more
pins at 0.5 mm or less, and only to signal nets whose other pads are all on
another layer (so a via is unavoidable). Per side, each pin runs straight out
and then at 45 degrees to its own via. The vias sit in a column at legal
spacing, assigned in pin order so no two fans cross. Each piece is checked
exactly and laid as fixed copper.

On bitaxe this lays 8 + 10 fans, with via columns 4.7 and 5.4 mm out at
1.01 mm spacing. Measured with seeds 1, 4 and 7 on all 13 boards, budget 600 s,
against step 4 (runs s1/s4/s7):
- bitaxe-ultra open connections went from 2/3/3 to **0/0/0**. Time went from
  307/289/288 s to 169/157/165 s. Blocking stays 4 on seeds 1 and 4, and goes
  from 4 to 5 on seed 7 (one starved thermal). bitaxe stays unaccepted: 3
  "footprint type mismatch" errors are already on the unrouted board.
- No other board was affected by the fanout. soleil seed 1 went from 1 open to 0
  (run-to-run variation).
- Totals over 39 runs: accepted 21 vs 21, unconnected 1 vs 10, blocking 402 vs
  401, 2021 s vs 2539 s.
- Smoke test (demo, practice, Prod_v2): all accepted.

### Known limits

- Coupled pairs: one layer per trunk, no paired vias inside the trunk, no
  polarity swap; pairs that cannot be coupled route as two nets (reported).
  Delay matching covers name-detected pairs only (no length groups), and no
  widths are changed for impedance.
- Through vias only. No blind, buried or micro vias. BGA escape covers walled-in
  inner balls of square-pitch arrays only (no staggered arrays, no channel or
  lane planning, escape vias at the class size). The fine-pitch fanout covers
  axis-aligned straight rows only, with one via column per side (no staggered
  columns), and only nets that must change layer.
- Custom rules beyond the subset above (area-specific or pairwise values are
  applied net-wide on the safe side), footprint copper text, teardrops and
  thermal-relief spokes are not modelled; KiCad DRC catches the misses. Starved
  thermals are the most common new DRC error.
- A plane that belongs to a net with no second plane never carries signals, which
  is what holds back soleil, mikoto and bitaxe. Checking plane continuity after
  routing would lift that.
- Existing tracks are obstacles, not part of the net. Existing vias are
  connected like pads.
- Outer-layer copper pours are not trusted to connect pads; those nets are
  routed with tracks.
- Speed: the search loop is pure Python, roughly 100,000 steps per second.
  Dense boards take minutes. A compiled kernel is the obvious next step.
- The grid costs density: with 0.2 mm tracks and 0.13 mm clearance, tracks pack
  at 0.4 mm, not 0.33 mm.
- Tested on Windows with KiCad 10.0.6 only. `tests/test_vela_engine.py` covers
  loader geometry, a brute-force clearance check, plane drops, mixed planes and
  neck-down.

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
