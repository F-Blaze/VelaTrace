# Real-board regression gate

`tests/test_regression_boards.py` runs the light audit checks (`run_rules` plus the offline BOM checks, no
network, no KiCad) on a small set of real open-hardware boards and compares the result with answer keys written
by blind reviewers. It runs in the normal `pytest` job on every PR.

| | what fails the PR |
|---|---|
| Layer 2, per board | a finding in `expected.json` that the answer key marks correct is no longer produced; or a finding appears that is not in `expected.json` |
| Layer 3, whole set | precision or recall falls below `floors.json` |

Precision is correct findings / judged findings (a finding is judged when it matches an issue or an
`acceptable_designs` entry; unmatched ones are "disputed" and not counted). Recall is in-scope answer-key issues
found / in-scope issues; `other:*`, `hs.*`/`fit.*` (heavy checks) and low-confidence entries are ignored. Matching
is the bench's: same rule or rule family and overlapping refs/nets. Run `pytest -s tests/test_regression_boards.py`
to see the numbers.

## Layout

- `boards/<slug>/` - `netlist.xml`, the upstream `LICENSE`, and `SOURCE.md` (URL, commit, licence).
- `answers/<slug>.json` - the answer key (`issues`, `acceptable_designs`), copied from the bench; disputed
  findings that were later settled by a blind adjudicator are merged in (`"source": "blind adjudication"`).
- `expected.json` - snapshot of the current findings per board. `floors.json` - minimum precision and recall.

## When the gate fails

- **A correct finding disappeared**: fix the regression. Do not edit `expected.json` to hide it.
- **A new finding**: look at it. If it is a false positive, fix the rule. If it is real (or at least reasonable),
  add it to the board's answer key (`issues`) or mark the design acceptable (`acceptable_designs`), then run
  `python tools/update_regression.py` and commit `expected.json`. State the reason in the PR description.
- **Precision/recall below the floor**: fix the check. Lower a floor only with a written reason in the PR. After
  a real improvement, `python tools/update_regression.py --floors` raises the floors; commit them.

Answer keys may be extended with new evidence, but never edited just to make a check pass.

## Adding a board

Only boards whose licence allows redistribution in an MIT repository without share-alike: **MIT, Apache-2.0,
BSD, CERN-OHL-P-2.0, CC-BY-4.0**. No GPL, CERN-OHL-S/W or CC-BY-SA, and no board with mixed or unclear licensing.
Check the shipped `LICENSE`, not just the README. Copy only `netlist.xml` (`kicad-cli sch export netlist --format
kicadxml`, remove the local directory from the `<source>` line) and the `LICENSE`, add `SOURCE.md`, write
`answers/<slug>.json` from an independent review of the schematic (not from VelaTrace's output), then run
`python tools/update_regression.py --floors`. Keep boards small (the whole set stays under a few MB).
