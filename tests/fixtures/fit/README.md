Trimmed test data for the footprint-fit check. Each file is the LCSC/EasyEDA component endpoint
response for the part named in its file name, captured 2026-10-20, cut down to the fields the
parser reads (part title, LCSC number, package title, footprint origin and the `PAD` records).
Not the full vendor responses.

- C65114: 2 x 3 pin header, 2.54 mm, through-hole
- C7593: NE555 in SOIC-8
- C2040: RP2040 in QFN-56 with exposed pad
- C124378: 1 x 4 pin header, 2.54 mm, through-hole
- digikey_keyword_example.json: HAND-WRITTEN from the field names of DigiKey's Product Information v4 keyword-search Swagger schema (invented part numbers, three products). It is NOT a captured DigiKey response; no real V4 response has been seen.
