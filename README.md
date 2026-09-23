# VelaTrace

VelaTrace is a Python companion for KiCad 9+ that audits component necessity and cost using real connectivity, and orchestrates external Freerouting with validation, previews and backups. It uses the official IPC client, never legacy SWIG/`pcbnew` bindings.

**Development alpha: no signed release exists.** The public repository is [F-Blaze/VelaTrace](https://github.com/F-Blaze/VelaTrace). Protected `main` contains the MIT license baseline; the application is awaiting independent PR review. This local checkout is for development and review, not production installation. Live KiCad editor transactions and several requested capabilities remain release gates. See [build status](docs/BUILD_STATUS.md) and the [completed release review](docs/RELEASE_REVIEW.md).

F-Blaze is the solo maintainer. There is no support or response-time guarantee. The [MIT license](LICENSE), present from the first commit, lets anyone use, modify and fork VelaTrace. Plugin and Content Manager listing is deferred until the features are stable.

## Behavior and current limits

- Audit requires a project description, reads PCB pad/net connectivity or a saved schematic/XML netlist, infers functions, and waits for corrections and confirmation before classification. Categories are **critical** (the design as built fails or becomes unsafe without it), **important** (quality/reliability suffers), **nice-to-have** (comfort, aesthetic or marginal benefit) and **redundant** (duplicates another function). Every verdict is a suggestion; it never removes components.
- Redundancy requires matching value, footprint/type and pin connectivity, with role and proximity checks. Sharing a rail alone is insufficient. Ambiguous cases say **possible redundancy — verify**. Code computes flags, Decimal totals, hypothetical savings and token counts; the model supplies judgments only.
- Classification displays the actual prompt's input-token count and output cap for approval. Pricing has separate consent based on the flagged count, eight-second searches and visible estimate fallback. Calls have a hard budget, output caps and at most two retries. The live usage display concerns the current response; unavailable exact streaming counts remain pending.
- `/autoroute` and `/autoroute_exit` switch the same window between modes. Session constraints stack across retries; universal constraints persist locally. Numeric constraints require confirmation. Freerouting chooses paths.
- Current routing accepts **saved, fully placed, initially unrouted boards**, straight traces, through vias, and all-net minimum width/clearance. Fresh manual DSN export is required. Existing routing, hierarchical schematic context, header keepouts and per-net constraints are refused. Incomplete routing stops; the stackup is never changed. Rejecting requires a reason.
- KiCad's IPC provides neither a dockable panel nor transient canvas overlays. VelaTrace uses an always-on-top external window and temporary `User.9` items. Panel previews are dashed violet; KiCad controls the user-layer color. Approved copper uses normal layer colors. Theme matching reads saved Windows KiCad settings when possible, with manual light/dark fallback.

The [feasibility report](docs/feasibility.md), [UI guide](docs/ui.md) and [routing boundaries](docs/routing-adapters.md) describe these fallbacks and limitations.

## Install checklist for a future signed release

These steps become usable when an independently reviewed release tag and trusted signing identity are published. **Never install from `main`, an unsigned tag, or this development branch.** No release tag or maintainer signing fingerprint is available yet.

1. Install [KiCad](https://www.kicad.org/download/) 9 or newer and Python 3.11 or newer. Routing requires the `kicad-cli` version to exactly match the connected editor, including its patch version. Live editor compatibility still needs the acceptance tests below.
2. Clone outside KiCad's plugin directory. Verify the chosen signed release against a maintainer key/fingerprint obtained through a trusted independent channel, then check out that tag. A signature from an unknown key is insufficient. Example PowerShell commands, after replacing the placeholder:

   ```powershell
   $releaseTag = 'REPLACE_WITH_PUBLISHED_SIGNED_TAG'
   git clone --no-checkout https://github.com/F-Blaze/VelaTrace.git VelaTrace
   Set-Location VelaTrace
   git fetch --tags origin
   git verify-tag $releaseTag
   if ($LASTEXITCODE -ne 0) { throw 'Release signature verification failed' }
   # Independently compare the reported signer with the trusted release identity.
   git checkout --detach $releaseTag
   if ($LASTEXITCODE -ne 0) { throw 'Release checkout failed' }
   ```

3. Put the verified checkout in `${KICAD_DOCUMENTS_HOME}/<version>/plugins/VelaTrace`, with `plugin.json` immediately inside `VelaTrace`. Typical roots are `Documents/KiCad` on Windows/macOS and `~/.local/share/KiCad` on Linux; use your version folder such as `10.0`. KiCad creates a separate Python environment and installs `requirements.txt`: **kicad-python 0.8.0** and **PySide6-Essentials 6.10.2**. Wait for dependency installation before looking for the action. [Official IPC installation](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/).
4. In **Preferences → Plugins**, enable **Enable KiCad API** and select a Python 3.11+ interpreter. Open the PCB Editor and use **Open VelaTrace**. Saved schematic analysis is selected inside the companion. [KiCad preferences](https://docs.kicad.org/10.0/en/kicad/kicad.html#_plugins_preferences).
5. Download the unmodified **Freerouting 2.1.0 JAR** from its [official release](https://github.com/freerouting/freerouting/releases/tag/v2.1.0) and install **Temurin Java 21** from [Adoptium](https://adoptium.net/temurin/releases/?version=21). Java 21 is mandatory even if a newer runtime is installed. Native tests used **21.0.12.1+1**. Check the JAR SHA-256 below; VelaTrace also verifies it and the offline policy at startup. Missing or mismatched tools visibly block startup. The GPLv3 router runs only as a separate process; its JAR/code is not bundled. See [runtime instructions](docs/freerouting.md).

   ```text
   2c07d58f75dac03782664081e7a58b41c25400d871a9fcf166a2ea6fe60d5def
   ```

6. In **Setup**, enter absolute paths to the JAR, Java 21 and matching `kicad-cli`. Choose the provider name, HTTPS base URL, protocol and an exact currently available model ID. Enter the API key there; it stays in memory. Gemini uses protocol `gemini` and `https://generativelanguage.googleapis.com/v1beta`. Groq uses protocol `openai` and `https://api.groq.com/openai/v1`. [Groq endpoint](https://console.groq.com/docs/openai).
7. Gemini supplies exact `countTokens`. Groq/OpenAI-compatible classification requires the optional `tokenizer` dependency, a locally installed tokenizer/chat template, and verification against that exact provider/model's usage. Install `.[tokenizer]` with the Python executable in **the environment that launches the plugin**; a separate terminal environment does not change KiCad's managed environment. Enter **Local tokenizer folder** and **Verified tokenizer model ID** in Setup. Nothing downloads a tokenizer at runtime; entering a model ID alone is not verification. Without a verified tokenizer, use Gemini's count endpoint or keep classification blocked.
8. For canvas annotations/previews, save the board and `.kicad_pro`, enable and show **User.9**, and set its KiCad color to `#8B5CF6` for violet graphics. Place all footprints before routing. Use a disposable project copy for live acceptance tests.

Setup paths and provider settings apply to the current launch. Optional defaults: `VELATRACE_FREEROUTING_JAR`, `VELATRACE_JAVA`, `VELATRACE_KICAD_CLI`, `VELATRACE_MODEL`, `VELATRACE_API_KEY`. Prefer entering the key in Setup; never put it in the repository or shared scripts. See [provider setup and privacy](docs/privacy.md).

## Using the companion

Enter a description, read the open PCB or select a saved schematic/XML netlist, and infer functions. Correct the cards, confirm functions, review the classification token estimate, then approve classification. Price the flagged/borderline set after its separate estimate. Missing manufacturer part numbers show **estimate only — no part number found**. Bulk and decoupling capacitors on one rail are not interchangeable duplicates.

For routing, enter `/autoroute`, edit session/universal rules using the violet constraint badge, and confirm every numeric interpretation. Request a fresh DSN export in the panel, export DSN from KiCad, and load that new file. Run routing and inspect the preview/validation summary before rejecting with a reason or approving. Reconfirm the full constraint list on each retry. Unsupported constraints stop the run so you can explicitly revise them.

Before every live board mutation, including preview creation/cleanup, the writer backs up the saved and live board/project under `.velatrace/backups` beside the board. Malformed/unsupported SES, stale state, incomplete DRC or backup failure refuses the operation. Approval uses one IPC commit intended to be one Ctrl+Z; live editor verification is still required. Approval leaves the board unsaved for normal KiCad saving. **Do not save while temporary graphics are present.** Clear them by closing/rejecting normally. After a crash or uncertain transaction, inspect the backup/journal and KiCad state before retrying; automatic crash cleanup is not implemented. Read [write safety](docs/write-safety.md).

## Privacy

Bring your own API key. **No backend, no telemetry:** the plugin only sends remote API requests to your configured endpoint. Before first analysis, a notice names the provider and explains that board data is sent with your key. Avoid confidential/NDA designs unless you trust that provider. Changing the endpoint or relevant settings requires fresh consent. Provider-side search follows its own processing terms; local safety backups retain design data.

Groq or Gemini offer free tiers subject to availability and quotas. **Gemini's free tier may use your prompts to improve their models; Groq's free tier does not make this claim.** Read the [Groq data policy](https://console.groq.com/docs/your-data) and [Gemini API terms](https://ai.google.dev/gemini-api/terms), including retention exceptions.

Gemini analysis works, but its web-search pricing is disabled until the required grounding presentation is implemented. Its pricing uses local illustrative estimates with zero search calls. Groq built-in search requires a supported search model chosen in Setup; search failure/timeout shows a visible reason and estimate fallback. Missing/invalid keys and outages produce explicit errors.

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

Latest local validation: **107 tests and 66 subtests passed, zero skips**, with pinned SDK, offscreen Qt, real Freerouting and official KiCad 10.0.6 CLI fixture checks. Native tests require the paths in [testing instructions](docs/testing.md); otherwise those two tests skip. The real CLI fixture intentionally has violations; this does not certify a fully routed candidate. No live provider or live KiCad editor write/undo acceptance has been performed.

**F-Blaze: enable two-factor authentication on the maintainer account.** Before publishing, enforce PR-only `main` with no maintainer bypass, independent review, required CODEOWNERS/CI/CodeQL checks, secret scanning and push protection, and signed tagged releases. A solo maintainer cannot approve their own PR: add a trusted second reviewer/code owner before merging. Local policy files do not enable GitHub settings. Main protection, secret scanning and push protection are enabled. Independent PR approval and release signing remain pending. See [remote setup status](docs/REMOTE_SETUP.md) for verified settings and workflow results. See [CONTRIBUTING](CONTRIBUTING.md) and [repository security setup](docs/repository-security.md).
