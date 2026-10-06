# VelaTrace demo board

A small, purpose-made KiCad 10 project for demonstrating [VelaTrace](https://github.com/F-Blaze/VelaTrace)
(offline design audit, BOM savings, one-click Freerouting with preview / DRC / approve).

**This is a demo, not a product. It contains deliberately planted design mistakes. Do not manufacture it.**

## The board

USB-C powered I2C temperature + RTC node, 2 layers, 50 x 35 mm, 23 parts, unrouted (no tracks, no zones).
Only stock KiCad library symbols and footprints (0603 passives, SOT-23, SOIC-8, SOD-123, through-hole connectors).

| Block | Parts |
|---|---|
| USB-C power input (power only, 5.1k on CC1/CC2, series Schottky) | J1, R1, R2, D3 |
| 3.3 V LDO (MCP1700) and output capacitor | U1, C1 |
| Power LED | D1 |
| LM75B I2C temperature sensor, alert pull-up | U3, C3, R7 |
| DS3231MZ I2C real-time clock, 1 Hz LED on SQW, backup battery connector (JST PH) | U4, C4, C5, R8, D2, J3 |
| I2C pull-ups (two sets) | R3, R4, R5, R6 |
| I2C header (GND, 3V3, SDA, SCL, ALERT) | J2 |
| Test points (GND, 3V3) | TP1, TP2 |

Files: `velatrace-demo.kicad_pro`, `velatrace-demo.kicad_sch`, `velatrace-demo.kicad_pcb`.

## Planted issues (what the audit should find)

| # | Planted mistake | VelaTrace rule | Shown as |
|---|---|---|---|
| 1 | Power LED D1 sits straight across +3V3 / GND with no series resistor | `led.no_resistor` | Error |
| 2 | LED D2 still has the symbol default value "LED" | `value.missing` | Warning |
| 3 | SDA and SCL each have two pull-ups (R3 + R5, R4 + R6) | `i2c.pullup.redundant` (x2) | Saving |
| 4 | The same capacitor is written "100n" (C4, C5) and "0.1uF" (C3) | `bom.value_normalise` | Saving |
| 5 | Alert pull-up R7 is 4.7k while 5.1k (R1, R2) is already on the BOM | `bom.value_merge` | Saving |
| 6 | Battery connector J3 feeds VBAT with no reverse-polarity / ESD part | `connector.power_unprotected` | Note |
| 7 | LDO U1 has no input capacitor on +5V | `decoupling.missing` (beta rule) | Note |

Everything else is meant to be clean: 8 findings in total, nothing unplanned.

## Notes for recording

- All DRC checks are enabled in the project (VelaTrace refuses to route while any check is set to "ignore"),
  and the User.9 layer is enabled (VelaTrace draws its routing preview there).
- Net class Default: 0.2 mm track, 0.2 mm clearance, 0.6 / 0.3 mm via.
- The unrouted board has no DRC errors other than the expected unconnected items.
- Freerouting 2.1.0 routes it 100 % in roughly 10-20 s, normally with zero or one via.

## License

MIT
