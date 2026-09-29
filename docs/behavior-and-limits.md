# Behavior, current limits and project security

## Behavior and current limits

- Audit requires a project description, reads PCB pad/net connectivity or a saved schematic/XML netlist, infers functions, and waits for corrections and confirmation before classification. Categories are **critical** (the design as built fails or becomes unsafe without it), **important** (quality/reliability suffers), **nice-to-have** (comfort, aesthetic or marginal benefit) and **redundant** (duplicates another function). Every verdict is a suggestion; it never removes components.
- Redundancy requires matching value, footprint/type and pin connectivity, with role and proximity checks. Sharing a rail alone is insufficient. Ambiguous cases say **possible redundancy — verify**. Code computes flags, Decimal totals, hypothetical savings and token counts; the model supplies judgments only.
- Classification displays the actual prompt's input-token count and output cap for approval. Pricing has separate consent based on the flagged count, eight-second searches and visible estimate fallback. Calls have a hard budget, output caps and at most two retries. The live usage display concerns the current response; unavailable exact streaming counts remain pending.
- `/autoroute` and `/autoroute_exit` switch the same window between modes. Session constraints stack across retries; universal constraints persist locally. Numeric constraints require confirmation. Freerouting chooses paths.
- Current routing accepts **saved, fully placed, initially unrouted boards**, straight traces, through vias, and all-net minimum width/clearance. Fresh manual DSN export is required. Existing routing, hierarchical schematic context, header keepouts and per-net constraints are refused. Incomplete routing stops; the stackup is never changed. Rejecting requires a reason.
- KiCad's IPC provides neither a dockable panel nor transient canvas overlays. VelaTrace uses an always-on-top external window and temporary `User.9` items. Panel previews are dashed violet; KiCad controls the user-layer color. Approved copper uses normal layer colors. Theme matching reads saved Windows KiCad settings when possible, with manual light/dark fallback.

The [feasibility report](feasibility.md), [UI guide](ui.md) and [routing boundaries](routing-adapters.md) describe these fallbacks and limitations.


## Audit and routing walkthrough (detail)

Enter a description, read the open PCB or select a saved schematic/XML netlist, and infer functions. Correct the cards, confirm functions, review the classification token estimate, then approve classification. Price the flagged/borderline set after its separate estimate. Missing manufacturer part numbers show **estimate only — no part number found**. Bulk and decoupling capacitors on one rail are not interchangeable duplicates.

For routing, enter `/autoroute`, edit session/universal rules using the violet constraint badge, and confirm every numeric interpretation. Request a fresh DSN export in the panel, export DSN from KiCad, and load that new file. Run routing and inspect the preview/validation summary before rejecting with a reason or approving. Reconfirm the full constraint list on each retry. Unsupported constraints stop the run so you can explicitly revise them.

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
