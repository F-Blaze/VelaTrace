# Full install checklist

The [README quickstart](../README.md#quickstart) is the short version.

**Alpha.** Verified on Windows with KiCad 10.0.4 / 10.0.6 only. VelaTrace writes to your board: read the code first and try it on a copy.

## A. Recommended: Plugin and Content Manager

1. Install [KiCad](https://www.kicad.org/download/) 10. You do not need to install Python: on Windows KiCad uses its own bundled Python.
2. In KiCad open the **Plugin and Content Manager**, search for **VelaTrace**, click **Install**, then **Apply Pending Changes**.
   Until the listing is approved: download `VelaTrace-v0.1.0-alpha.2-pcm.zip` from the [latest release](https://github.com/F-Blaze/VelaTrace/releases) and use **Install from File…**. To check the download, compare its SHA-256 (`Get-FileHash .\VelaTrace-v0.1.0-alpha.2-pcm.zip` in PowerShell) with the one in the release notes.
   If you installed earlier with `git clone` (path B), delete that `Documents/KiCad/10.0/plugins/VelaTrace` folder first so KiCad does not load two copies.
3. During the install KiCad 10.0.6 asks "This plugin requires the KiCad API, which is currently disabled in preferences. Would you like to enable it?": click **Yes**. If you clicked No, tick **Enable KiCad API** in **Preferences → Plugins**.
4. Open the PCB Editor. First start only: KiCad creates a separate Python environment and installs `requirements.txt` (**kicad-python 0.8.0**, **PySide6-Essentials 6.10.2**, **numpy 2.3.5**; about 300 MB on disk). The VelaTrace button appears when that is done; this took about 4 minutes on our Windows test PC. [Official IPC installation notes](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/).

### Troubleshooting

- **No button.** **Preferences → Plugins**, right-click VelaTrace, **Recreate Plugin Environment**, and wait for the install to finish.
- **Installing the packages fails** (offline, proxy or firewall). The environment is built with `pip`, which needs access to PyPI. Fix the connection, then use **Recreate Plugin Environment**.
- **"Cannot connect to KiCad".** The API is not enabled: tick **Enable KiCad API** in **Preferences → Plugins**.
- **A message that numpy is missing.** Routing stays locked and a red hint says so. Use **Recreate Plugin Environment** so KiCad installs `requirements.txt` again.
- **`kicad-cli` not found.** Routing needs a `kicad-cli` that exactly matches the running editor, patch version included. VelaTrace looks for it on `PATH` and in the folder of KiCad's bundled Python (where the Windows installer puts it). Otherwise enter its absolute path in **Setup**.

## B. Fallback: manual install from a signed tag

Use this if you cannot use the Plugin and Content Manager. It needs Python 3.11 or newer: in **Preferences → Plugins** pick a Python 3.11+ interpreter.

The release tag is signed with the maintainer's SSH key, fingerprint `SHA256:komvC+jlhCr/yBGl/RA6BDSLwOMYT6uYdK8cpzpxVyw`; GitHub shows the tag as Verified. This file is not an independent channel for that fingerprint, so compare it with what GitHub shows for the tag.

1. Clone outside KiCad's plugin directory. Verify the chosen signed release against a maintainer key/fingerprint obtained through a trusted independent channel, then check out that tag. A signature from an unknown key is insufficient. Example PowerShell commands:

   ```powershell
   $releaseTag = 'v0.1.0-alpha.2'
   git clone --no-checkout https://github.com/F-Blaze/VelaTrace.git VelaTrace
   Set-Location VelaTrace
   git fetch --tags origin
   git verify-tag $releaseTag
   if ($LASTEXITCODE -ne 0) { throw 'Release signature verification failed' }
   # Independently compare the reported signer with the trusted release identity.
   git checkout --detach $releaseTag
   if ($LASTEXITCODE -ne 0) { throw 'Release checkout failed' }
   ```

2. Put the verified checkout in `${KICAD_DOCUMENTS_HOME}/<version>/plugins/VelaTrace`, with `plugin.json` immediately inside `VelaTrace`. On Windows the root is `Documents/KiCad`; use your version folder such as `10.0`. Linux and macOS are untested. KiCad creates the Python environment as in path A.
3. Tick **Enable KiCad API** in **Preferences → Plugins**, open the PCB Editor and use **Open VelaTrace**.

## After install

1. Routing needs no Java and no extra download: **Route board** uses Vela-routing, which runs inside the plugin. If numpy is missing, routing stays locked and the red hint says so.
2. In **Setup**, enter the absolute path to a matching `kicad-cli` if it is not found on its own. Choose the provider name, HTTPS base URL, protocol and an exact currently available model ID. Enter the API key there; it stays in memory. Gemini uses protocol `gemini` and `https://generativelanguage.googleapis.com/v1beta`. Groq uses protocol `openai` and `https://api.groq.com/openai/v1`. [Groq endpoint](https://console.groq.com/docs/openai).
3. Gemini supplies exact `countTokens`. Groq/OpenAI-compatible classification requires the optional `tokenizer` dependency, a locally installed tokenizer/chat template, and verification against that exact provider/model's usage. Install `.[tokenizer]` with the Python executable in **the environment that launches the plugin**; a separate terminal environment does not change KiCad's managed environment. Enter **Local tokenizer folder** and **Verified tokenizer model ID** in Setup. Nothing downloads a tokenizer at runtime; entering a model ID alone is not verification. Without a verified tokenizer, use Gemini's count endpoint or keep classification blocked.
4. For canvas annotations/previews, save the board and `.kicad_pro`, enable and show **User.9**, and set its KiCad color to `#8B5CF6` for violet graphics. Place all footprints before routing. Use a disposable project copy for live acceptance tests.
5. **Before routing, enable every DRC check.** KiCad's default project sets five checks to *Ignore* (`footprint_filters_mismatch`, `footprint_type_mismatch`, `missing_courtyard`, `track_not_centered_on_via`, `tuning_profile_track_geometries`). VelaTrace refuses to route while any check is ignored. In **Board Setup → Design Rules → Violation Severity**, set them to *Warning* and save the project. Errors and newly introduced warnings block approval. Identifiable pre-existing warnings are reported without blocking; incomplete identities make every candidate issue blocking.

Setup paths and provider choices are saved locally; API keys stay in memory for the current launch. Optional defaults: `VELATRACE_KICAD_CLI`, `VELATRACE_MODEL`, `VELATRACE_API_KEY`. Prefer entering the key in Setup; never put it in the repository or shared scripts. See [provider setup and privacy](privacy.md).
