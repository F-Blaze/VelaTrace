# External companion window

Run `python -m velatrace` or the KiCad IPC action to open one always-on-top
window, initially positioned at the right edge of the primary display. Move it
beside KiCad. KiCad's public IPC interface does not expose docking or the native
window rectangle. VelaTrace never uses legacy `pcbnew` bindings.

Routing has two separate actions: **Generate routing preview**, then **Approve and
apply copper**. User.9 graphics are non-copper and do not clear the ratsnest.
Approval uses the validated route without rerunning Freerouting. Blocking DRC
types are shown separately from pre-existing warnings. The routing page scrolls
so approval remains reachable on smaller screens.

The User.9 preview is drawn as soon as Freerouting's result is imported. Candidate
KiCad DRC then runs on a separate background thread while the status reads
**Checking DRC…**; approval stays disabled until it finishes. Reject and
**Generate routing preview** stay usable meanwhile: rejecting, rerouting, changing
mode or loading new input makes the running check stale, and its result is
discarded (the routing session's generation counter). If DRC fails, or the board
changed between drawing the preview and validating it, the preview is removed and
the route cannot be approved. Closing waits for a running DRC check.

**Approve anyway…** is a separate, red, non-default button, enabled
only when a complete preview (zero unconnected items) is blocked by known DRC
findings. Its confirmation lists the blocking count and the first five DRC issue
types, states that copper will be written despite them, and defaults to Cancel.
It bypasses only the DRC gate: backup, the single undoable commit, board-unchanged,
stale-preview and preview-collision checks, and the Freerouting JAR pin all still
apply, and incomplete routes or unknown DRC still refuse. The override and the
blocking issues are written to that commit's `intent.json` and `completion.json`,
and the result line says the route was applied despite DRC errors.

The status shows the current stage and elapsed seconds. Finished runs show
routing, preview and DRC durations separately. Successful approval reports track,
via and layer counts after reading the copper back from KiCad. Failed approval
retains the panel preview with an explicit error; that is not a new routing run.
An uncertain result blocks retry until inspected.

Before Freerouting starts, **Generate routing preview** checks User.9. Old
VelaTrace previews (saved into the board, restored by Undo, or left by an earlier
run) are recognised by the UUIDs in `.velatrace/backups/*/completion.json` and
removed through the normal backed-up transaction. Dashed 0.1 mm User.9 lines that
no journal proves are VelaTrace's are never deleted: routing refuses immediately and
lists their coordinates so you can delete or move them. If a new preview segment
still coincides with a foreign User.9 line, preview creation refuses before writing
(KiCad can replace a coincident line) and names the lines. Never save temporary
previews into the board.

Copper text can cause clearance errors if the router crosses it. Use silkscreen
for printed labels; intentionally copper lettering needs routing clearance. Zone
filling is separate from track routing and is not automatically performed by
approval. VelaTrace does not create ground planes or change the stackup.

Setup verifies the exact external Freerouting JAR and Java runtime before enabling
routing. The audit page works without them; while routing is locked, a red line
at the top of the Route page names the reason (including the last Setup error) and points to
**Setup**. See [Freerouting setup](freerouting.md). Tool paths, provider and model
choices are remembered in `settings.json` in the local configuration directory and
are kept for a retry if verification fails. The API key is never written to a
config file; it lasts for the current launch only. Environment variables `VELATRACE_FREEROUTING_JAR`, `VELATRACE_JAVA`,
`VELATRACE_KICAD_CLI`, `VELATRACE_MODEL` and `VELATRACE_API_KEY` can supply defaults.
Do not place secrets in repository files or shared launch scripts. Gemini requires
an exact currently available model ID. OpenAI-compatible/Groq classification
additionally requires a locally installed tokenizer and chat template verified
against that precise provider/model. Nothing downloads a tokenizer at runtime.

Launch opens no dialog. If Setup already saved a Freerouting JAR, the router is
re-verified quietly in the background; otherwise the red hint explains how to
unlock routing. The audit needs no setup at all.

Audit: choose the open PCB through official IPC, a saved schematic exported
through `kicad-cli`, or a previously exported KiCad XML netlist, then click
**Check design**. The built-in checks run locally with no key and no dialog
(picking a saved file is the confirmation that it is saved; the step line notes
that unsaved edits are not included). Findings are grouped under Errors,
Warnings, Savings and Info with a colored bar and a one-line title; clicking a
card expands its evidence, fix and illustrative cost. The summary line reads like
`3 errors · 2 warnings · est. $0.42/board saving`. A reserved hook
(`MainWindow.zoom_to_part`) will add a **Show in KiCad** link per finding.

**Explain with AI (optional)** starts the provider flow below, with the findings as
context. The small description box is optional and locked once AI starts;
**New audit** clears findings, temporary annotations and the AI pass. Inference
produces editable function and electrical-role cards.
**Confirm functions** is an inline button; it applies your corrections and counts
the actual classification prompt. The count, output cap and call limit then appear
in one cost line beside **Classify parts**; clicking it is the consent. Pricing
works the same way: its estimate for the actual flagged set is shown beside
**Price flagged parts**, and if what would run changed since it was shown, the
click only refreshes the line. The only AI dialog is the one-time privacy notice.
The usage display identifies the current response, not a billed run aggregate.
When a provider does not stream exact usage, it shows `pending` and received
characters until counts arrive. The hard shared API-call budget is independent.

Cards show bucket text plus a colored bar, part reference/value, function, optional
price and `est.` tag. **Suggestion +** expands plain text in place. All model
and board text uses Qt plain-text labels; hyperlinks and rich text are not
interpreted. Audit annotations are attempted automatically for IPC boards after
classification, with a refresh button. Failure is visible. They use guarded
temporary `User.9` items and require a saved board/project and enabled user layer.
Schematic-only audits cannot annotate the PCB canvas.

Click **Route** (Ctrl+2) to change the same window to Routing; **Audit** (Ctrl+1) returns to
Audit and clears owned temporary graphics. **Edit…** beside the constraint line opens
session and universal rules. Universal rules live in the platform's local
application configuration directory as `constraints.json`; session rules remain
only in memory and stack across retries. **Add rule** turns text into a numeric
constraint, which then appears in the constraint line. Header/per-net constraints are retained but currently refuse
routing rather than being ignored. The currently supported baseline is all-net
clearance and width on saved, placed, initially unrouted boards.

The fresh DSN handshake is explicit because KiCad 9/10's pinned IPC and CLI do
not expose DSN export. Request an export, export in KiCad, then load the freshly
written file. The complete numeric constraint list is shown beside **Generate
routing preview**; each click confirms it (Ctrl+Enter also works). Freerouting and the preview run on the service worker; candidate CLI
DRC follows on its own background worker.
The panel draws a separate dashed-violet preview for each used copper layer;
board graphics are dashed on `User.9`, whose color remains under KiCad's control.
Set that user layer to violet manually if desired. Preview failure disables
approval. Verified counts and completion are shown without inventing percentages.
Reject requires a reason; manually adjust numeric constraints before retrying.
Approved copper uses the real KiCad layer colors through the guarded writer.

All blocking operations run serially on one persistent thread, including IPC
connection creation and subsequent access, except candidate DRC, which runs on a
second persistent thread; its board reads go through the same `BoardSafety` lock. Results and token updates return to
Qt's main thread. Closing during work refuses until it completes; failed cleanup
keeps the window open and displays the error. The application never silently
discards an uncertain board-write outcome. See [write safety](write-safety.md) for
backup, undo and live KiCad acceptance requirements.

The look is frosted glass: translucent cards with a light rim over a violet-indigo
(dark) or lilac (light) gradient, glossy violet primary buttons, and a pill mode
badge. Popups, tooltips and dialogs stay solid for legibility, and disabled controls
are visibly greyed. Real Windows 11 Acrylic is not used: Windows removes it from
stay-on-top windows, and dark Acrylic renders as near-opaque grey. Light and dark
palettes share violet `#8B5CF6`. KiCad's IPC has no live theme
notification. The default **KiCad** menu choice reads the connected version's
saved Windows `appearance.app_theme` setting when available, then falls back to
the OS palette. The menu also provides a manual override; custom KiCad palettes
cannot be reproduced exactly. Changes are refreshed when reconnecting/reading.

For a synthetic, disconnected visual preview:

```sh
python -m velatrace --demo
python -m velatrace --demo --screenshot docs/ui-demo.png
```

Set `QT_QPA_PLATFORM=offscreen` for render-only environments. Demo mode performs
no provider calls, IPC access or board writes. The screenshot is explicitly
synthetic; it is not evidence of live KiCad integration. Qt is supplied by the
separate, unmodified `PySide6-Essentials` dependency, subject to its own LGPL/GPL/
commercial licensing terms; VelaTrace application code is MIT.
