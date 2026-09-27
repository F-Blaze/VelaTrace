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
   Obtain a fresh DSN and normal placement/constraint confirmations.
3. Run `session.run(trusted_via_catalog(dsn))`, display `session.summary`, then
   `safety.show_preview(dsn, session.plan)` if a plan is available. A shortfall may
   be previewed but never approved.
4. Reject with an explicit reason and call `safety.clear_preview()` before the
   next attempt. Clear owned annotations before obtaining a new saved-board DSN.
5. Call `session.approve(writer)` only after the user's approval. Its single
   transaction removes the preview and adds the candidate copper. Do not call
   `board.save()` afterward: saving is the user's explicit KiCad action.
6. On normal close or `/autoroute_exit`, clear temporary graphics. If cleanup
   refuses or IPC is uncertain, display the error and backup path; do not silently
   dismiss the window or retry a copper commit.

The current supported baseline is a saved, initially unrouted board with a matching
saved `.kicad_pro`, straight routed segments and ordinary through vias. Existing
tracks, arc tracks or vias cause a refusal; VelaTrace never guesses which existing
copper it should replace. Hierarchical schematic context, blind/buried/microvias,
per-net constraints and header keepouts currently refuse explicitly. These are
capability limits, not successful completion of those requirements.

The candidate preserves the complete source PCB text and appends the verified
copper items. Unknown source board geometry is preserved. The real board's nets
and copper layers are checked; the same integer-nanometre coordinates, widths and
UUIDs are used for candidate serialization and IPC construction. Via diameter and
drill must match saved project settings as well as DSN circle geometry. A padstack
name alone is not trusted. There is no stackup setter.

The candidate is local temporary data under the project `.velatrace/backups`
directory. It copies the corresponding project, rules and non-hierarchical
schematic context. The validator binds both contents and absence of optional
files, refuses DRC exclusions/ignored checks, and invokes official `kicad-cli`
DRC. The routing UI requires an exact editor/CLI version match before creating the
routing session. Matching saved schematic context must export successfully before
DRC explicitly enables schematic parity; malformed context refuses validation.
For confirmed extra clearance it runs both the original rules and a second
pass with a global minimum rule; adding a weaker global rule cannot erase evidence
from the original stronger rules. Trace-width minima are also measured in code.
Unknown, nonzero or failed DRC prevents approval. The writer accepts only the exact
evidence produced by its paired validator, then consumes it after application.

Before every live mutation, including annotation creation, preview creation,
rejection cleanup and approval, VelaTrace creates new immutable copies of the
saved board and the live board using official `SaveCopyOfDocument`. Copies never
replace a source file. Live project JSON is compared with saved project JSON;
missing live project backup or changed settings prevent mutation. Whole-board
canonical comparison includes all geometry, footprints, pads, fields, zones and
unknown contents. Only generator metadata, harmless numeric spelling and root
item ordering are normalized. Unexplained differences stop the operation.

A durable `intent.json` lists exact item UUIDs before a transaction begins; a
`completion.json` records successful commit acknowledgment. Saved board and
project digests are checked again immediately before and during the transaction.
All requested additions must return their exact IDs and protobuf geometry.
Partial creation or failed deletion drops the entire transaction. A failed
rollback or ambiguous begin/commit response blocks retries and reports the backup
directory: inspect the actual KiCad state before restarting the plugin. An
acknowledged rollback is not claimed when the connection is lost.

`show_preview(dsn, plan)` creates dashed non-copper graphics on an already enabled
`User.9` layer; it never changes enabled layers or layer colors. `show_annotations`
accepts plain `(text, x_mm, y_mm)` rows. `clear_preview()` removes only exact IDs
created in this service instance, after checking that they have not been edited.
Other user-layer contents are never swept. KiCad owns board-layer colors; set
User.9 to violet in KiCad if desired. The separate panel can always draw violet.

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
checked offline. An isolated official KiCad 10.0.6 CLI runtime passed the authored
fixture checks for expected violations/unconnected items and refusal of disabled
checks; see [testing.md](testing.md). KiCad is not installed system-wide, and no
live editor IPC session has been tested. Real
candidate DRC, SaveCopy project naming, server attribute normalization, reads during
an open commit, and actual one-step Ctrl+Z still require live KiCad 9+ acceptance
testing before release. Exact protobuf comparison may safely refuse a server that
adds default attributes; do not weaken it without recording those verified defaults.
