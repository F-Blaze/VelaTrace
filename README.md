# VelaTrace

**Free and local: audit your KiCad design, find BOM savings, and autoroute safely — nothing leaves your machine, undo anytime.**

[![License: MIT](https://img.shields.io/github/license/F-Blaze/VelaTrace)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/F-Blaze/VelaTrace/ci.yml?branch=main&label=CI)](https://github.com/F-Blaze/VelaTrace/actions/workflows/ci.yml)
[![CodeQL](https://img.shields.io/github/actions/workflow/status/F-Blaze/VelaTrace/codeql.yml?branch=main&label=CodeQL)](https://github.com/F-Blaze/VelaTrace/actions/workflows/codeql.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![KiCad 9+](https://img.shields.io/badge/KiCad-9%2B%20(tested%2010.0)-314cb0)](https://www.kicad.org/)

<!-- HERO GIF: docs/hero.gif -->

<p align="center">
  <img src="docs/ui-demo.png" alt="VelaTrace audit: offline findings grouped as errors, warnings and savings, with a per-board saving estimate" width="340">
  &nbsp;
  <img src="docs/ui-demo-routing.png" alt="VelaTrace routing: Freerouting preview per layer with reject and approve" width="340">
</p>
<p align="center"><sub>Screenshots use the built-in synthetic demo (<code>python -m velatrace --demo</code>): no API, IPC or board writes.</sub></p>

VelaTrace is a companion window for KiCad's PCB Editor. Its mission is to save hardware designers time and money, and it is free: the checks run offline, AI is optional, and there is no paid service behind it.

## Features

- **Offline design audit.** Rule checks on real pad/net connectivity: missing or distant decoupling capacitors, I2C pull-ups (missing, redundant, wrong value), LEDs without a series resistor, single-pin nets, floating IC inputs, duplicated ICs, unprotected power connectors, 0 Ω shorts and unset values. No key, no network. [Rule list](docs/behavior-and-limits.md#built-in-audit-checks)
- **BOM savings.** Consolidates equivalent values and packages (`100n` / `0.1uF` / `100nF`) and, with an opt-in, free JLCPCB parts list, flags Extended parts that have a Basic equivalent. [Details](docs/bom.md)
- **Optional AI explanation.** Bring your own key (Gemini or any OpenAI-compatible endpoint) for plain-language explanations. Token counts and a hard call budget are shown first; the key stays in memory.
- **Shareable offline report.** **Save report** writes one self-contained HTML file (no scripts, no external requests). [Details](docs/report.md)
- **One-click routing.** **Route board** runs pre-flight checks, exports the DSN, runs [Freerouting](https://github.com/freerouting/freerouting), draws a `User.9` preview, then validates it with KiCad DRC in the background. **Approve** (or **Approve anyway**, after a confirmation that lists the findings) writes the copper as a single undoable commit, after a backup. **Cancel routing** stops a run.
- **Optional warm router.** Opt in to keep one verified Freerouting JVM running so repeat routes start faster.
- **Safe by construction.** Suggestions only: VelaTrace never deletes a component. Backup before every live write, KiCad's official IPC API only (no SWIG/`pcbnew`), no backend, no telemetry. [Write safety](docs/write-safety.md)

## Comparison

| | VelaTrace | Stock Freerouting KiCad plugin | Cloud AI routers |
|---|---|---|---|
| Runs where | Your machine | Your machine | Vendor's cloud (board is uploaded) |
| Cost | Free, MIT | Free | Varies by service |
| Preview before applying | Yes (`User.9` layer) | Not built in | Varies |
| DRC gate before approval | Yes (background KiCad DRC; explicit override) | Not built in | Varies |
| Single undo of the result | Yes (one IPC commit, backup first) | Not built in | Varies |
| Design audit and BOM savings | Yes, offline | No | Varies |

This reflects our reading of each tool's public documentation at the time of writing; "varies" means it differs between services. Freerouting itself does the actual routing in both local options. Corrections welcome via an issue.

## Audit accuracy

The audit favours silence over guessing: a check stays quiet when the data cannot show a problem clearly. How often it is right on real boards is being measured.

**Status: beta.** Measured in October 2026 against blind expert reviews of open-source KiCad boards: 91% of findings were correct on the 37 boards used to tune the rules, but only 44% on 12 further boards the rules had never seen. So three rules that caused most wrong findings (`decoupling.missing`, `i2c.pullup.missing`, `pin.input_floating`) are shown as Info notes for now, and the rest keep their normal severity. Planted-fault tests catch removed decoupling capacitors, missing or duplicate I2C pull-ups and LEDs without a resistor. Treat findings as a second pair of eyes, not a sign-off.

Found a wrong or missing finding? Please file an [audit false positive](https://github.com/F-Blaze/VelaTrace/issues/new?template=audit_false_positive.yml) report; they directly improve the rules.

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

**Audit.** Pick the open PCB (or a saved schematic/XML netlist) and click **Check design**. Built-in checks run on your computer with no key, no network and no dialogs, and list findings as errors, warnings, savings and info, e.g. `3 errors · 2 warnings · est. $0.02/board saving`. Click a finding for its evidence, fix and cost. They cover missing or distant decoupling capacitors, I2C pull-ups (missing, redundant, wrong value), LEDs without a series resistor, single-pin nets, floating IC inputs, duplicated ICs, unprotected power connectors and unset or 0 Ω values ([details and limits](docs/behavior-and-limits.md#built-in-audit-checks)). Optional: **Explain with AI** sends the findings and design to your own provider for plain-language explanations and part roles, after a one-time privacy notice; each AI step shows its token and call cost beside the button, and clicking it is the consent. The hard call budget always applies. Pricing the flagged parts is a further opt-in click.

**Route.** Click **Route** at the top (Ctrl+2), add any rules (the list shown beside **Route board** is confirmed by the click), then click **Route board**. VelaTrace reads the open board (no save needed), checks it (outline, footprints outside it, duplicate references, existing copper), exports the DSN with KiCad's own bundled Python and runs Freerouting, showing each stage with **Cancel routing**. If KiCad cannot export automatically, it offers **Load DSN exported from KiCad…** instead. The User.9 preview appears as soon as Freerouting finishes; KiCad DRC then runs in the background and **Approve** unlocks when it passes. Then **Reject** (with a reason) or **Approve** (the click is the confirmation). If DRC findings block approval, **Approve anyway** applies the route after a confirmation that lists them; the override is recorded in the backup journal. Approval swaps the preview for real copper in one IPC commit, leaving the board unsaved so a single Undo reverts it. Currently supported: placed (saved at least once), initially unrouted boards with straight traces, through vias and all-net width/clearance.

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
