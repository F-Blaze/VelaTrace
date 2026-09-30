# Full install checklist

The complete, verified install path. The [README quickstart](../README.md#quickstart) is the short version.

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
5. Download the unmodified **Freerouting 2.1.0 JAR** from its [official release](https://github.com/freerouting/freerouting/releases/tag/v2.1.0) and install **Temurin Java 21** from [Adoptium](https://adoptium.net/temurin/releases/?version=21). Java 21 is mandatory even if a newer runtime is installed. Native tests used **21.0.12.1+1**. Check the JAR SHA-256 below; VelaTrace also verifies it and the offline policy at startup. Missing or mismatched tools visibly block startup. The GPLv3 router runs only as a separate process; its JAR/code is not bundled. See [runtime instructions](freerouting.md).

   ```text
   2c07d58f75dac03782664081e7a58b41c25400d871a9fcf166a2ea6fe60d5def
   ```

6. In **Setup**, enter absolute paths to the JAR, Java 21 and matching `kicad-cli`. Choose the provider name, HTTPS base URL, protocol and an exact currently available model ID. Enter the API key there; it stays in memory. Gemini uses protocol `gemini` and `https://generativelanguage.googleapis.com/v1beta`. Groq uses protocol `openai` and `https://api.groq.com/openai/v1`. [Groq endpoint](https://console.groq.com/docs/openai).
7. Gemini supplies exact `countTokens`. Groq/OpenAI-compatible classification requires the optional `tokenizer` dependency, a locally installed tokenizer/chat template, and verification against that exact provider/model's usage. Install `.[tokenizer]` with the Python executable in **the environment that launches the plugin**; a separate terminal environment does not change KiCad's managed environment. Enter **Local tokenizer folder** and **Verified tokenizer model ID** in Setup. Nothing downloads a tokenizer at runtime; entering a model ID alone is not verification. Without a verified tokenizer, use Gemini's count endpoint or keep classification blocked.
8. For canvas annotations/previews, save the board and `.kicad_pro`, enable and show **User.9**, and set its KiCad color to `#8B5CF6` for violet graphics. Place all footprints before routing. Use a disposable project copy for live acceptance tests.
9. **Before routing, enable every DRC check.** KiCad's default project sets five checks to *Ignore* (`footprint_filters_mismatch`, `footprint_type_mismatch`, `missing_courtyard`, `track_not_centered_on_via`, `tuning_profile_track_geometries`). VelaTrace refuses to route while any check is ignored. In **Board Setup → Design Rules → Violation Severity**, set them to *Warning* and save the project. Errors and newly introduced warnings block approval. Identifiable pre-existing warnings are reported without blocking; incomplete identities make every candidate issue blocking.

Setup paths and provider choices are saved locally; API keys stay in memory for the current launch. Optional defaults: `VELATRACE_FREEROUTING_JAR`, `VELATRACE_JAVA`, `VELATRACE_KICAD_CLI`, `VELATRACE_MODEL`, `VELATRACE_API_KEY`. Prefer entering the key in Setup; never put it in the repository or shared scripts. See [provider setup and privacy](privacy.md).

