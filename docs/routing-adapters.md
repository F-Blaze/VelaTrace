# Routing adapter contracts

`RoutingSession` defaults to Audit, handles `/autoroute` and `/autoroute_exit`, and
requires explicit completed-placement and numeric-constraint confirmations. Session
constraints survive retries; only universal constraints are atomically saved to the
local config path. Changing either scope invalidates confirmation. Empty rejection
reasons block retries even if another input is edited. Rejection records a reason;
the caller must obtain and confirm concrete numeric adjustments instead of guessing.

KiCad's pinned official API has no DSN export operation. Request `ExportTicket.begin`
after the user saves the board, then have them export DSN from KiCad. `accept_export`
requires a post-request file timestamp and explicit save/export confirmation. It
matches board basename, saved digest, layer names, pin-to-net membership, reference
set and placement coordinates. These checks do not prove pad rotation, keepouts or
pad geometry equivalence; the actual candidate must pass DRC against the real board.
Any subsequent DSN or saved-board digest change invalidates the export. The live
adapter must separately refuse unsaved edits or a different open board.

The external process adapter implements `Router.check_startup()` and
`Router.route(dsn, constraints) -> SES text`. It must honor the confirmed numeric
constraints through checked router settings or an appropriately validated DSN copy.
It must not transmit data to any router service, modify the stackup, or accept
AI-produced trace geometry. `RoutingSession` checks startup when constructed.

`parse_ses` supports straight polyline wires and circular vias mapped through a
trusted `ViaSpec` catalog derived by the board adapter. It verifies `library_out`
circle diameters/layers against that catalog. SES alone cannot supply a drill
mapping. Coordinates in `RoutePlan` are millimetres with Specctra Y-up; invert Y
exactly once in the KiCad adapter. Trace count is number of segments, computed in
code. Unsupported geometry is refused as a whole, including arcs and placement
changes. Actual Freerouting output must be tested against this grammar before
declaring that router version supported. The parser accepts standard literal
`(string_quote ")` metadata. Additional output metadata needs explicit validation,
not silent removal.

`CandidateValidator.supports(constraints)` must return false for any numeric
constraint it cannot enforce. `validate(dsn, plan, constraints)` must create a
candidate copy based on the exact board, apply the entire plan to that copy, run
real KiCad DRC including unconnected checks, verify every numeric constraint and
return `ValidationReport`. Bind `plan_digest(plan)` and
`dsn.ticket.board_digest` in the report. Unknown counts are `None`; a log line or
unverified router success status is not evidence. Fractional routing percentages
require counted routed and total connections, never an LLM estimate. Unknown
completion and nonzero unconnected counts stop routing. Approval also requires
zero verified DRC violations. No stackup changes occur here.

`BoardWriter.apply(dsn, plan, report)` is the sole board mutation boundary. It must
recheck current live and saved board identities/geometry, evidence digests, backup
before every write, preflight all supported geometry before opening a commit, and
apply as one official IPC undoable commit. No direct SES import API is assumed.
Preview graphics and cleanup must use the same reviewed safety boundary and must
never be mistaken for the copper plan. A UI-only preview requires no board write.

Upstream interchange reference: [Freerouting project](https://github.com/freerouting/freerouting).
