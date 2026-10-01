# VelaTrace

**Catch design mistakes, cut your BOM cost and autoroute safely — inside KiCad. Free offline checks, optional bring-your-own-key AI, no telemetry, every change previewed and undoable.**

[![License: MIT](https://img.shields.io/github/license/F-Blaze/VelaTrace)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/F-Blaze/VelaTrace/ci.yml?branch=main&label=CI)](https://github.com/F-Blaze/VelaTrace/actions/workflows/ci.yml)
[![CodeQL](https://img.shields.io/github/actions/workflow/status/F-Blaze/VelaTrace/codeql.yml?branch=main&label=CodeQL)](https://github.com/F-Blaze/VelaTrace/actions/workflows/codeql.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![KiCad 9+](https://img.shields.io/badge/KiCad-9%2B%20(tested%2010.0)-314cb0)](https://www.kicad.org/)

<!-- TODO: hero GIF of audit → route preview → approve -->

<p align="center">
  <img src="docs/ui-demo.png" alt="VelaTrace audit: offline findings grouped as errors, warnings and savings, with a per-board saving estimate" width="340">
  &nbsp;
  <img src="docs/ui-demo-routing.png" alt="VelaTrace routing: Freerouting preview per layer with reject and approve" width="340">
</p>
<p align="center"><sub>Screenshots use the built-in synthetic demo (<code>python -m velatrace --demo</code>): no API, IPC or board writes.</sub></p>

VelaTrace is a companion window for KiCad's PCB Editor. It does two things:

1. **Audit** — reads real pad/net connectivity and runs free, offline design checks (decoupling, I2C pull-ups, LED resistors, dangling nets, duplicated parts and more) with concrete fixes and savings. An optional AI pass explains the findings.
2. **Route** — runs [Freerouting](https://github.com/freerouting/freerouting) on your placed board and lets you preview, validate and approve the result before a single track touches your copper.

## Why VelaTrace

- **Connectivity-aware, not guesswork.** Checks and AI classification start from actual pad/net connectivity (open PCB, or a saved schematic/XML netlist), not just reference designators. Two parts on the same rail are not called duplicates unless value, footprint and pin connectivity match.
- **Suggestions, never deletions.** Verdicts are advice. VelaTrace never removes a component; you approve everything.
- **Code does the math.** Flags, Decimal totals, hypothetical savings and token counts are computed in code. The model supplies judgments only.
- **Safe routing pipeline.** Preview on `User.9` → DRC-validate against your live board (only errors and newly introduced warnings block; pre-existing warnings are reported) → approve → one undoable IPC commit. Rejecting requires a reason.
- **Backup before every write.** The saved board, live board and project are copied to `.velatrace/backups` beside your board before any live change, including preview creation and cleanup.
- **Bring your own key.** Gemini or any Groq/OpenAI-compatible endpoint. Token counts and a hard call budget are shown and confirmed before each call.
- **No backend, no telemetry.** Requests go only to the endpoint you configure. Freerouting runs as a separate process with network access denied.
- **Official API only.** Uses KiCad's IPC API via [kicad-python](https://pypi.org/project/kicad-python/), never legacy SWIG/`pcbnew` bindings.

## Quickstart

> **Status: 0.1.0a1, development alpha.** No signed release exists yet, and the maintainer recommends reviewing the code before installing. Live preview, cleanup and approval were accepted on KiCad 10.0.4/10.0.6. Details: [build status](docs/BUILD_STATUS.md), [release review](docs/RELEASE_REVIEW.md).

**Requirements:** KiCad 9+ (tested on 10.0.4 / 10.0.6) with the IPC API enabled, and Python 3.11+. For routing also: Temurin **Java 21** and the **Freerouting 2.1.0** JAR.

1. **Install the plugin.** Clone into KiCad's plugin folder so `plugin.json` sits directly inside `VelaTrace`:

   ```sh
   # Windows: Documents/KiCad/10.0/plugins   Linux: ~/.local/share/KiCad/10.0/plugins
   git clone https://github.com/F-Blaze/VelaTrace.git VelaTrace
   ```

   KiCad creates a Python environment and installs `kicad-python 0.8.0` and `PySide6-Essentials 6.10.2` itself; wait for that to finish. To verify a signed release once one exists, use the [full install checklist](docs/install.md).
2. **Enable the API.** In KiCad **Preferences → Plugins**, tick **Enable KiCad API** and pick a Python 3.11+ interpreter. Open the PCB Editor and click **Open VelaTrace**.
3. **Optional: add an AI key.** The audit's checks need none. For **Explain with AI**, choose a provider and model in **Setup** and enter your key (kept in memory only).
   Gemini: protocol `gemini`, `https://generativelanguage.googleapis.com/v1beta`. Groq: protocol `openai`, `https://api.groq.com/openai/v1`.
4. **For routing only:** install [Temurin Java 21](https://adoptium.net/temurin/releases/?version=21) and download the unmodified [Freerouting 2.1.0 JAR](https://github.com/freerouting/freerouting/releases/tag/v2.1.0). VelaTrace checks the JAR's SHA-256 (`2c07d58f…60d5def`) at startup; enter the JAR, Java and a `kicad-cli` path in **Setup**. `kicad-cli` must match the running editor exactly, patch version included. See [Freerouting setup](docs/freerouting.md).
5. **Before routing:** save the board and project once (later edits need no save), enable **User.9** for previews, and set every DRC check to at least *Warning* (VelaTrace refuses to route while any is set to *Ignore*; KiCad's defaults ignore five). Try it on a disposable copy first.

Want to look around first? `pip install -e '.[dev]'` then `python -m velatrace --demo` runs the UI on synthetic data.

## Usage

**Audit.** Pick the open PCB (or a saved schematic/XML netlist) and click **Check design**. Built-in checks run on your computer with no key, no network and no dialogs, and list findings as errors, warnings, savings and info, e.g. `3 errors · 2 warnings · est. $0.02/board saving`. Click a finding for its evidence, fix and cost. They cover missing or distant decoupling capacitors, I2C pull-ups (missing, redundant, wrong value), LEDs without a series resistor, single-pin nets, floating IC inputs, duplicated ICs, unprotected power connectors and unset or 0 Ω values ([details and limits](docs/behavior-and-limits.md#built-in-audit-checks)). Optional: **Explain with AI** sends the findings and design to your own provider for plain-language explanations and part roles, with the existing consent, token and budget confirmations; pricing the flagged parts is a further opt-in step.

**Route.** Enter `/autoroute` in the box at the top and add any constraints (numeric ones need confirmation). Click **Route board**: VelaTrace reads the open board (no save needed), checks it (outline, footprints outside it, duplicate references, existing copper), exports the DSN with KiCad's own bundled Python and runs Freerouting, showing each stage with **Cancel routing**. If KiCad cannot export automatically, it offers **Load DSN exported from KiCad…** instead. The User.9 preview appears as soon as Freerouting finishes; KiCad DRC then runs in the background and **Approve** unlocks when it passes. Inspect the preview and DRC summary, then **Reject** (with a reason) or **Approve**. If DRC findings block approval, **Approve anyway** applies the route after a confirmation that lists them; the override is recorded in the backup journal. Approval swaps the preview for real copper in one IPC commit, leaving the board unsaved so a single Undo reverts it. Currently supported: placed (saved at least once), initially unrouted boards with straight traces, through vias and all-net width/clearance.

Full behavior and limits: [docs/behavior-and-limits.md](docs/behavior-and-limits.md). Transaction safety: [docs/write-safety.md](docs/write-safety.md). UI guide: [docs/ui.md](docs/ui.md).

## Privacy

No backend, no telemetry. A notice names your provider and explains that component fields, connectivity and your description are sent with your key; it repeats whenever you change the endpoint or search settings. Avoid confidential/NDA designs unless you trust the provider. Note that Gemini's free tier may use prompts to improve its models; Groq's free tier does not make this claim. Provider policies and key handling: [docs/privacy.md](docs/privacy.md).

## Contributing

Contributions, forks and bug reports are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md), and look for [good first issues](https://github.com/F-Blaze/VelaTrace/labels/good%20first%20issue). Tests need no key or running KiCad:

```sh
python -m pip install -e '.[dev]'
python -m ruff check src tests launch.py
python -m pytest
```

Security reports: see [SECURITY.md](SECURITY.md). VelaTrace is solo-maintained by F-Blaze with no response-time guarantee.

## License

[MIT](LICENSE). Freerouting is GPLv3 and is run as a separate, unmodified external process; its JAR is not bundled.
