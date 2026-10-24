Trimmed test data for the footprint-fit check. Each file is the LCSC/EasyEDA component endpoint
response for the part named in its file name, captured 2026-10-20, cut down to the fields the
parser reads (part title, LCSC number, package title, footprint origin and the `PAD` records).
Not the full vendor responses.

- C65114: 2 x 3 pin header, 2.54 mm, through-hole
- C7593: NE555 in SOIC-8
- C2040: RP2040 in QFN-56 with exposed pad
- C124378: 1 x 4 pin header, 2.54 mm, through-hole

Captured 2026-10-21 (same trimming), the megadesk parts whose KiCad footprints differ only in land pattern:
- C614879: ATTINY841-SSUR in SOIC-14 (rows 5.60 mm apart; KiCad 4.95)
- C152207: MCP2003B-E/SN in SOIC-8 (rows 5.41 mm apart)
- C353288: SMF33A in SOD-123FL (pads 3.30 mm apart; KiCad SOD-123F 2.80)
- C391035: FST-9018 buzzer (pads 8.0 mm apart; KiCad Murata PKMCS0909E 8.7)
- C116325: 78M12 in TO-252-2 (tab pad 2)
- C16106: HT7550-1 in SOT-89-3 (polygon tab pad 2, 10 x a pin's area)
- C668117: F1004-H-10-20G-R 10-pin FFC connector, mounting pads numbered 11 and 12
- digikey_keyword_example.json: HAND-WRITTEN from the field names of DigiKey's Product Information v4 keyword-search Swagger schema (invented part numbers, three products). It is NOT a captured DigiKey response; no real V4 response has been seen.
