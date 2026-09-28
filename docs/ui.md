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

The status shows the current stage and elapsed seconds. Finished runs show
routing, DRC and preview durations separately. Successful approval reports track,
via and layer counts after reading the copper back from KiCad. Failed approval
retains the panel preview with an explicit error; that is not a new routing run.
An uncertain result blocks retry until inspected.

If new preview segments coincide with existing User.9 graphics, preview creation
refuses before writing: KiCad can replace an existing coincident line. Remove only
unwanted old preview graphics yourself; the plugin does not assume everything on
User.9 belongs to it. Never save temporary previews into the board.

Copper text can cause clearance errors if the router crosses it. Use silkscreen
for printed labels; intentionally copper lettering needs routing clearance. Zone
filling is separate from track routing and is not automatically performed by
approval. VelaTrace does not create ground planes or change the stackup.

Setup verifies the exact external Freerouting JAR and Java runtime before enabling
routing. The audit page works without them; while routing is locked, a red line
under the header names the reason (including the last Setup error) and points to
**Setup**. See [Freerouting setup](freerouting.md). Tool paths, provider and model
choices are remembered in `settings.json` in the local configuration directory and
are kept for a retry if verification fails. The API key is never written to a
config file; it lasts for the current launch only. Environment variables `VELATRACE_FREEROUTING_JAR`, `VELATRACE_JAVA`,
`VELATRACE_KICAD_CLI`, `VELATRACE_MODEL` and `VELATRACE_API_KEY` can supply defaults.
Do not place secrets in repository files or shared launch scripts. Gemini requires
an exact currently available model ID. OpenAI-compatible/Groq classification
additionally requires a locally installed tokenizer and chat template verified
against that precise provider/model. Nothing downloads a tokenizer at runtime.

Audit starts with a required design description. Choose the open PCB through
official IPC, a saved schematic exported through `kicad-cli`, or a previously
exported KiCad XML netlist with connectivity. The captured description is locked
after reading; **New audit** clears temporary annotations and allows a new
description. Inference produces editable function and electrical-role cards.
Explicit function confirmation precedes token counting; the actual prompt's
precise input count and output cap need a second confirmation before
classification. Pricing has its own confirmation for the actual flagged set.
The usage display identifies the current response, not a billed run aggregate.
When a provider does not stream exact usage, it shows `pending` and received
characters until counts arrive. The hard shared API-call budget is independent.

Cards show bucket text plus a colored bar, part reference/value, function, optional
price and `est.` tag. **Show suggestion** expands plain text in place. All model
and board text uses Qt plain-text labels; hyperlinks and rich text are not
interpreted. Audit annotations are attempted automatically for IPC boards after
classification, with a refresh button. Failure is visible. They use guarded
temporary `User.9` items and require a saved board/project and enabled user layer.
Schematic-only audits cannot annotate the PCB canvas.

Type `/autoroute` and press **Enter** to change the same window to Routing; `/autoroute_exit` returns to
Audit and clears owned temporary graphics. The violet constraint badge opens
session and universal rules. Universal rules live in the platform's local
application configuration directory as `constraints.json`; session rules remain
only in memory and stack across retries. Numeric interpretations require
confirmation. Header/per-net constraints are retained but currently refuse
routing rather than being ignored. The currently supported baseline is all-net
clearance and width on saved, placed, initially unrouted boards.

The fresh DSN handshake is explicit because KiCad 9/10's pinned IPC and CLI do
not expose DSN export. Request an export, export in KiCad, then load the freshly
written file. The complete numeric constraint list needs confirmation before
each route. Freerouting and actual candidate CLI DRC run on the service worker.
The panel draws a separate dashed-violet preview for each used copper layer;
board graphics are dashed on `User.9`, whose color remains under KiCad's control.
Set that user layer to violet manually if desired. Preview failure disables
approval. Verified counts and completion are shown without inventing percentages.
Reject requires a reason; manually adjust numeric constraints before retrying.
Approved copper uses the real KiCad layer colors through the guarded writer.

All blocking operations run serially on one persistent thread, including IPC
connection creation and subsequent access. Results and token updates return to
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
