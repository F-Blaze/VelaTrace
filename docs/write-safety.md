# Board safety boundary

`BoardSafety(board, saved_board_path)` is the only live mutation service. Use the
official `reader.client.get_board()` object; no SWIG module is involved.
`SafeCandidateValidator(safety, cli)` implements the routing validator protocol;
`SafeBoardWriter(safety, validator)` implements the final approval writer. Pass
`trusted_via_catalog(dsn)` into `RoutingSession.run()` before parsing router output.
All methods are synchronous: serialize UI work and do not edit the board/project
in KiCad while an operation is running.

UI integration order:

1. Construct `safety = BoardSafety(reader.client.get_board(), snapshot.path)`,
   `validator = SafeCandidateValidator(safety, cli)` and
   `writer = SafeBoardWriter(safety, validator)`.
2. Construct the routing session with that validator and the external router.
   Call `safety.prepare_preview()` (removes old previews), read
   `text = board.get_as_string()`, run `preflight` on it, then
   `dsn = export_live(cli, text, snapshot, folder)`; `session.set_input(dsn, ...)`.
   The manual fallback instead takes `ExportTicket.begin(...)` before the user
   exports, and `accept_export(...)` after; both pass identical DSN checks.
3. Run `plan = session.route(trusted_via_catalog(dsn))` (stage `validating`), note
   `session.generation`, and draw `snapshot = safety.show_preview(dsn, plan,
   canonical(parse(text)))` — bound to the exported board text, so an edit made
   after the click refuses the preview (manual path: no expected board). Then run
   `session.check(plan)` off the UI thread, require `validator.evidence[5] == snapshot`
   (the board did not change after the preview was drawn), and hand the report (or
   `None` on failure) to `session.accept(plan, report, generation)` on the UI thread.
   `accept` returns False for a stale result after a reroute, rejection or
   invalidation. `session.run()` does route, check and accept in one call. A
   shortfall may be previewed but never approved.
4. Reject with an explicit reason and call `safety.clear_preview()` before the
   next attempt. Clear owned annotations before obtaining a new saved-board DSN.
5. Call `session.approve(writer)` only after the user's approval. The explicit
   **Approve anyway** override is `session.approve(writer, drc_override=True)`: it lifts
   only the gate on known DRC violations. The writer records
   `{"drc_override": {"violations": n, "blocking_reasons": [...]}}` in that commit's
   `intent.json` and `completion.json`. Its single
   transaction removes the preview and adds the candidate copper. Do not call
   `board.save()` afterward: saving is the user's explicit KiCad action.
6. On normal close or when leaving Route mode, clear temporary graphics. If cleanup
   refuses or IPC is uncertain, display the error and backup path; do not silently
   dismiss the window or retry a copper commit.

The current supported baseline is a saved, initially unrouted board with a matching
saved `.kicad_pro`, straight routed segments and ordinary through vias. Existing
tracks, arc tracks or vias cause a refusal; VelaTrace never guesses which existing
copper it should replace. Hierarchical schematic context, blind/buried/microvias,
per-net constraints and header keepouts currently refuse explicitly. These are
capability limits, not successful completion of those requirements.

The candidate uses the current live PCB text, excludes only this session's verified
temporary item IDs, and appends the verified
copper items. Unknown source board geometry is preserved. The real board's nets
and copper layers are checked; the same integer-nanometre coordinates, widths and
UUIDs are used for candidate serialization and IPC construction. Via diameter and
drill must match saved project settings as well as DSN circle geometry. A padstack
name alone is not trusted. There is no stackup setter.

The candidate is local temporary data under the project `.velatrace/backups`
directory. It copies the corresponding project, rules and non-hierarchical
schematic context. The validator binds both contents and absence of optional
files, refuses DRC exclusions, runs checks the project sets to Ignore as warnings (in its temporary copy only), and invokes official `kicad-cli`
DRC.

Project files cannot switch the gate off. In the temporary copy only:
- a `(severity ignore)` rule in the `.kicad_dru` runs as a warning, like Board Setup's Ignore;
- when the `.kicad_dru` contains any rule, the unrouted board and the candidate are
  checked a second time **without that file**. Any issue the route adds in that pass
  blocks approval, listed as "hidden by this project's custom rules (.kicad_dru)"
  (an issue the pass under the project's rules also reports is counted once, there).
  Issues that pass already shows on the unrouted board are not the route's and do
  not block. This costs two more DRC runs, in parallel with the others, and is the
  actual proof: it does not depend on reading the untrusted rule file correctly.
  "Approve anyway" remains for a rule set that legitimately relaxes a check;
- a net class whose clearance is 0 or negative (and not raised by Board Setup's
  minimum clearance) is refused before routing: KiCad then skips the clearance and
  short checks for that class (measured with kicad-cli 10.0.6).

What this does not cover: issues already on the unrouted board that the project's
rules hide (KiCad's own DRC shows the same), and local clearance overrides stored
in the board file itself.

The DSN export adds a no-track/no-via keepout over every copper graphic and copper
text, including those inside footprints that have pads and a footprint's visible
reference/value on copper. Only a graphic that touches one of its own footprint's
pads (a net tie or antenna) gets none; candidate DRC rejects a route across it.
If the keepouts cannot be added, the export stops with the reason instead of
producing a DSN without them. The routing UI requires an exact editor/CLI version match before creating the
routing session. Matching saved schematic context must export successfully before
DRC explicitly enables schematic parity; malformed context refuses validation.
That export proof depends only on the exact schematic and project bytes, so it
runs once per content within a session rather than once per candidate copy.
The unrouted-board baseline DRC does not depend on the route: it runs while
Freerouting runs, and a baseline result is reused only for byte-identical board,
project, rules and schematic files. Validation still re-reads the live board and
project after routing; any difference simply runs the baseline again. Baseline and
candidate DRC passes run concurrently, each in its own temporary folder.
Each DRC uses a private, per-session copy of the user's KiCad settings
(`kicad_common.json` path variables and `sym-lib-table` copied verbatim) whose
footprint library table keeps only the libraries the board's footprints name.
KiCad 10 otherwise loads every configured footprint library for its
footprint-library checks, about 10 s per run with the stock libraries, although
only the board's own libraries can change the report. Reports were identical with
the full and the trimmed table. A table that cannot be trimmed exactly (a nested
table behind a path variable, or an unreadable one) is copied whole. The user's
own settings folder is only read.
For confirmed extra clearance it runs both the original rules and a second
pass with a global minimum rule; adding a weaker global rule cannot erase evidence
from the original stronger rules. Trace-width minima are also measured in code.
Unknown or failed DRC prevents approval. Every error and newly introduced warning
blocks approval unless the user explicitly overrides known DRC violations with
**Approve anyway**; unconnected items are never overridable. Identifiable pre-existing warnings are reported without blocking;
missing issue identities fall back to counting every candidate issue as blocking.
The writer accepts only the exact
evidence produced by its paired validator, then consumes it after application.

Before every live mutation, including annotation creation, preview creation,
rejection cleanup and approval, VelaTrace creates new immutable copies of the
saved board, the live board obtained through official `get_as_string()`, and the
saved project when present. Copies never replace a source file. The live board
copy is flushed to disk before a mutation. `SaveCopyOfDocument` is not used: it
can rewrite the real project and caused a KiCad 10.0.4 crash during earlier tests.

There is no requirement to save before routing or between preview and approval.
The one-click DSN is exported from the live board text read at click time, never
from the saved file, and VelaTrace never calls `board.save()`. The board must have
been saved once (a path, project folder and `.kicad_pro` must exist for backups
and rules). The saved file's digest is still bound into the export ticket, so
saving during routing refuses the result; the live chain is DSN text = preview
snapshot = DRC snapshot = approval snapshot. Candidate DRC uses
the live board, including unsaved edits made before validation. Approval compares
that validated snapshot with the live board, excluding only verified owned preview
items. Changes after validation require a new validation, not a save of the preview.
Whole-board canonical comparison includes footprints, pads, fields, zones and
unknown geometry. Generator metadata, harmless numeric spelling and root item
ordering are normalized. Edited or missing owned previews refuse explicitly.

Board Setup changes not present in the saved project cannot be read safely by this
adapter. Save the project rules before routing; the exported DSN's net classes and
validation both use those saved rules, and validation checks their digests. Do not edit the board or project
while a routing operation is running. This is not full live Board Setup validation.

A durable `intent.json` lists exact item UUIDs before a transaction begins; a
`completion.json` records successful commit acknowledgment. Saved board and
project digests are checked again immediately before and during the transaction.
The validated live snapshot is re-read after beginning the commit, before any
removal or addition, to catch edits made during backup/journal creation.
All requested additions must return their exact IDs and protobuf geometry.
Partial creation or failed deletion drops the entire transaction. A failed
rollback or ambiguous begin/commit response blocks retries and reports the backup
directory: inspect the actual KiCad state before restarting the plugin. An
acknowledged rollback is not claimed when the connection is lost.

`show_preview(dsn, plan, expected_board=None)` creates dashed non-copper graphics on an already enabled
`User.9` layer and returns the live-board snapshot it was drawn against; it never
changes enabled layers or layer colors. `show_annotations`
accepts plain `(text, x_mm, y_mm)` rows. `clear_preview()` removes only exact IDs
created in this service instance, after checking that they have not been edited.
Other user-layer contents are never swept. KiCad owns board-layer colors; set
User.9 to violet in KiCad if desired. The separate panel can always draw violet.

Before temporary segment creation, a collision check compares the layer and
undirected endpoints with nonowned existing graphics, and rejects duplicate
requested segments. `prepare_preview()` runs before routing: it verifies User.9,
adopts its own earlier graphics on that layer (earlier runs, saved or
Undo-restored previews) and removes them with the usual backup, then refuses
before routing if unrecorded dashed 0.1 mm User.9 lines remain.

An item is adopted only when both hold:
1. its UUID is recorded as drawn by VelaTrace. The record that counts is the
   journal in the user's VelaTrace settings folder (`journals/<hash of the board
   path>.json`, written before each temporary-graphics commit; the newest 50,000
   ids per board are kept). Records in the project's `.velatrace/backups/*/completion.json`
   travel with the project and can be written by anyone, so they are only believed
   for items that pass the stricter test below. They are still read so that
   previews drawn by releases before this journal existed are not stranded;
2. it looks like what VelaTrace draws on the preview layer: a dashed 0.1 mm
   segment, or text. For a project-folder record, text must also read exactly like
   an audit annotation (`REF: critical|important|nice-to-have|redundant`).

So a project that ships a forged record cannot make VelaTrace remove the author's
own drawings or notes; the most it can name are exact look-alikes of VelaTrace's
temporary graphics. `.velatrace` gets a `.gitignore` containing `*` when it is
created, so backups and records are not committed by accident.

**Retention.** Each safety check writes a backup folder (`<board>-<uuid>`: saved
board, live board, saved project). Only the newest **20** per board keep those
copies (`BoardSafety.keep_backups`; one route attempt makes about eight); older
copies are deleted when a new backup is written. The small `intent.json` and
`completion.json` of older transactions are kept as the audit trail (for example
of an "Approve anyway"). Bare-uuid folders from earlier releases are pruned by
the same rule. Private KiCad settings copies in the temp folder
(`velatrace-kicad-settings-*`, `velatrace-kicad-export-*`) are removed on normal
exit; ones a killed session left behind are removed at the next start once they
are a day old. A running session refreshes its folder on every use and rebuilds
it if it is gone. This prevents a verified KiCad behavior that replaces an
existing coincident User.9 line with the new UUID. The board-change check must not
ignore that disappearance. Approved copper is read back by UUID and exact geometry
after the commit; missing/mismatched copper blocks retries as an uncertain write.

Approval removes the owned preview and creates all approved copper inside one
`begin_commit` / `push_commit` pair. It leaves the board unsaved in KiCad; the user
saves normally. One KiCad Undo is intended to revert that whole commit, restoring
the preview. That undo invalidates the plugin's evidence; refresh before further
routing. Do not save a board while temporary graphics are present. Close/reject
must call `clear_preview()` first. If the plugin crashes, retained exact-ID
journals and live/saved board copies provide manual recovery; automatic crash
reconciliation is not implemented and no arbitrary layer cleanup is attempted.

## Verification limits

Failure-injection tests verify ordering, backup refusal, partial-create rollback,
failed-removal rollback, ambiguous IPC blocking, preview ownership, original and
supplemental DRC passes, changed project rules and single-commit application.
Official pinned SDK constructors for tracks, through vias, graphics and text are
checked offline. On 2026-09-27, the installed KiCad 10.0.4 editor and matching CLI
passed a disposable two-pad routing test with real Freerouting 2.1.0: zero DRC
violations/unconnected items, User.9 preview, approval without saving, live reads
inside the commit, preview removal, one-step Undo restoring the preview, and guarded
cleanup. Saved board bytes were unchanged. See [current review](internal/REVIEW_2026_09_27.md)
and [testing.md](testing.md). This is one supported geometry case, not blanket
KiCad-version or operating-system certification.
