# VelaTrace

**Check a KiCad board for small mistakes before you order it. A free KiCad 10 plugin: a fast first-pass check, a JLCPCB part-number check, and a board router (Vela-routing, VelaTrace's own) with preview, KiCad DRC check and one undo. Alpha, tested on Windows only.**

[![License: MIT](https://img.shields.io/github/license/F-Blaze/VelaTrace)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/F-Blaze/VelaTrace/ci.yml?branch=main&label=CI)](https://github.com/F-Blaze/VelaTrace/actions/workflows/ci.yml)
[![CodeQL](https://img.shields.io/github/actions/workflow/status/F-Blaze/VelaTrace/codeql.yml?branch=main&label=CodeQL)](https://github.com/F-Blaze/VelaTrace/actions/workflows/codeql.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![KiCad 10](https://img.shields.io/badge/KiCad-10%20(Windows%20only%20tested)-314cb0)](https://www.kicad.org/)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange)](#quickstart)

<p align="center">
  <img src="docs/hero.gif" alt="VelaTrace on a demo board in KiCad: Check design lists findings, an LED finding is expanded, Route board draws a preview, Approve applies the copper" width="820">
</p>
<p align="center"><sub>Recorded on the demo board in <a href="examples/velatrace-demo">examples/velatrace-demo</a>, which has deliberately planted issues. Waiting time is cut; the route took about 10 seconds.</sub></p>

<p align="center">
  <img src="docs/ui-demo.png" alt="VelaTrace audit: offline findings grouped as errors, warnings and savings, with a per-board saving estimate" width="340">
  &nbsp;
  <img src="docs/ui-demo-routing.png" alt="VelaTrace routing: route preview per layer with reject and approve" width="340">
</p>
<p align="center"><sub>Screenshots use the built-in synthetic demo (<code>python -m velatrace --demo</code>): no API, IPC or board writes.</sub></p>

Small mistakes are easy to skim past before ordering, and each one costs a re-order. Click **Check design** and in about a second you get a short list of things to look at, each with the evidence and a suggested fix. The check only reads your board and needs no account, key or network.

**What it has found.** With the optional JLCPCB parts list loaded, it found 48 capacitors labelled "10u" that all carried the part number of a 1 µF part on one open-source board, and seven resistors of four different values sharing one 1 k part number on another. The parts check has been tried on three boards so far.

**Try it on the demo board.** [examples/velatrace-demo](examples/velatrace-demo) has mistakes planted in it. Do the [Quickstart](#quickstart) install steps (the check needs no router setup), open the demo board and click **Check design**.

**How far to trust it.** It is a first pass, not a design review. On 12 boards it had never seen, 44% of the design-rule findings were judged correct, and the reviewers were LLM agents, not human engineers. See [Audit accuracy](#audit-accuracy).

VelaTrace is a companion window for KiCad's PCB Editor. It is free (MIT), offline by default (network use is opt-in and listed in [docs/privacy.md](docs/privacy.md)), and there is no backend or paid service behind it. It is an alpha: expect rough edges, and treat every result as a suggestion to check.

**Tested on:** Windows with KiCad 10.0.4 and 10.0.6. Linux and macOS are untested. KiCad 9 is untested and may not work. Development is AI-assisted; claims here are backed by tests or files in the repository.

## Features

- **Experimental offline rule check.** A small set of deterministic checks on pad/net connectivity, with no key and no network: LEDs without a series resistor, I2C pull-up value and rail problems, duplicated ICs, 0 Ω shorts, unset values and single-pin nets, among others. Four rules that were wrong too often (`decoupling.missing`, `i2c.pullup.missing`, `net.single_pin`, `pin.input_floating`) are shown as Info notes only. The check finds only a minority of the issues a reviewer would raise; see [Audit accuracy](#audit-accuracy). [Rule list](docs/behavior-and-limits.md#built-in-audit-checks)
- **BOM tidy-ups.** Value and package spelling and merges (`100n` / `0.1uF` / `100nF`). With an opt-in JLCPCB parts list it can also suggest Basic-part swaps; those suggestions are lightly tested. [Details](docs/bom.md)
- **JLCPCB parts (new, lightly tested).** An opt-in catalogue download (about 181 MB, 800 MB on disk; the public data file of the kicad-jlcpcb-tools project) gives an offline parts table with tier, stock and price. One click refreshes live stock and prices for the parts on your BOM. It flags LCSC numbers that do not match the part's value, flags out-of-stock parts, writes the LCSC field to the board in one undoable step, and exports JLCPCB BOM, placement and Gerber files. It cannot edit the schematic (a KiCad 10 limit). Tried on three boards so far. [Details](docs/jlcpcb.md)
- **Guarded routing with Vela-routing.** **Route board** runs pre-flight checks, exports the DSN, routes the board with Vela-routing, VelaTrace's own router (Python and numpy, inside the plugin; no Java or JAR), draws a `User.9` preview, then validates it with KiCad DRC in the background. **Approve** (or **Approve anyway**, after a confirmation that lists the DRC findings) writes the copper as a single undoable commit, after a backup. **Cancel routing** stops a run. It routes only placed, initially unrouted boards and refuses anything else (existing routing, per-net rules, header keepouts, hierarchical context). It is meant for small boards; see [Limits and alternatives](#limits-and-alternatives). [How Vela-routing works](docs/VELA_ROUTING.md)
- **Optional AI explanation.** Bring your own key (Gemini or any OpenAI-compatible endpoint) for plain-language explanations. Token counts and a hard call budget are shown first; the key stays in memory.
- **Shareable offline report.** **Save report** writes one self-contained HTML file (no scripts, no external requests). [Details](docs/report.md)
- **Write safety.** Backup before every live write; board reads and writes go through KiCad's official IPC API (the DSN export alone runs KiCad's bundled Python on a private copy). Crash recovery is manual. [Write safety](docs/write-safety.md)

## Limits and alternatives

**What it does not do.** Routing uses one track width and one clearance for all nets, including power. There is no differential-pair or length tuning and no BGA fanout. Dense boards usually end as a partial route you can reject. **Approve anyway** applies a route that failed KiCad DRC. DRC-clean does not mean electrically correct. For those things, look at [KiCadRoutingTools](https://github.com/drandyhaas/KiCadRoutingTools) (its own router, differential pairs, length matching, BGA/QFN fanout), [kicad-happy](https://github.com/aklofas/kicad-happy) (datasheet-aware design review) and [kicad-jlcpcb-tools](https://github.com/Bouni/kicad-jlcpcb-tools) (a mature JLCPCB workflow: part assignment saved into the schematic, parametric search, placement corrections). VelaTrace does not do what the first two do, and its JLCPCB window (below) is newer and narrower than the third.

**Vela-routing** is VelaTrace's own router and the engine behind **Route board**. It is
experimental. On VelaTrace's 13-board test set it was accepted by KiCad DRC in 20 of 39
runs, against 8 of 33 for the previous Freerouting path; a route that fails DRC is never
approved silently. See [how it works and what was measured](docs/VELA_ROUTING.md). The
electrical-routing research ([scope and benchmarks](docs/ELECTRICAL_ROUTING.md)) and the
[reference-plane experiment](docs/REFERENCE_ROUTING.md) still use Freerouting as their baseline.

## Offline by default

The audit and routing run on your computer. Network use is opt-in and limited to: optional AI provider calls (your own endpoint and key), an optional JLCPCB parts download from a public GitHub Pages site (`bouni.github.io` for the full catalogue, `lrks.github.io` for the small list), an optional live stock and price lookup that sends only LCSC part numbers to `cart.jlcpcb.com` (a JLCPCB website endpoint, not an official API), an optional footprint lookup in Run heavy checks that sends LCSC part numbers to `easyeda.com` (and, with your own API credentials, part numbers to `api.digikey.com`), and optional Groq search (which uses Tavily). Installation also downloads Python packages (including numpy for routing). No telemetry. Details: [docs/privacy.md](docs/privacy.md).

## Audit accuracy

The audit favours silence over guessing, and it is experimental. What was measured, exactly:

- The reviewers and the adjudicator were LLM agents (Claude Opus), not human experts. One human, the maintainer, decided 5 borderline cases.
- On 12 open-source boards the rules had never seen, 44% of findings were judged correct (27% if unadjudicated findings count as wrong).
- The tool surfaces only a minority of the issues the reviewers raised.
- Rules that were wrong too often are shown as Info notes (see Features).

Expect false positives and missed problems; treat findings as hints, not a sign-off. Found a wrong or missing finding? Please file an [audit false positive](https://github.com/F-Blaze/VelaTrace/issues/new?template=audit_false_positive.yml) report.

## Quickstart

> **Status: 0.1.0a2, alpha.** See the [latest release](https://github.com/F-Blaze/VelaTrace/releases/latest). Verified only on a handful of small boards, on Windows with KiCad 10.0.4 / 10.0.6.

**Requirements:** KiCad 10 (Windows is the only tested OS). Routing also needs a `kicad-cli` that matches the running editor exactly, patch version included; VelaTrace looks for it on `PATH` and next to KiCad's own Python.

1. **Install.** In KiCad open the **Plugin and Content Manager**, search for **VelaTrace**, click **Install**, then **Apply Pending Changes**. Until the listing is approved: download `VelaTrace-v0.1.0-alpha.2-pcm.zip` from the [latest release](https://github.com/F-Blaze/VelaTrace/releases) and use **Install from File…** in the Plugin and Content Manager. If you installed earlier with `git clone`, delete that `plugins/VelaTrace` folder first so KiCad does not load two copies. Read the code first if you like: it writes to your board.
2. **Allow the KiCad API.** During the install KiCad asks to enable the KiCad API: click **Yes**. (Missed it? **Preferences → Plugins**, tick **Enable KiCad API**.) KiCad uses its own bundled Python on Windows; there is nothing else to choose.
3. **Open the PCB Editor.** First start only: KiCad installs about 300 MB of Python packages (`kicad-python`, `PySide6`, `numpy`), then the VelaTrace button appears; this took about 4 minutes in our test. Click it, then **Check design**. Try it on the demo board first (`velatrace-demo-v0.1.0-alpha.2.zip` from the release).

No button? **Preferences → Plugins**, right-click VelaTrace, **Recreate Plugin Environment**. More help and a manual install: [docs/install.md](docs/install.md).

- **Routing** needs no Java. Before routing: save the board and project once (later edits need no save), enable **User.9** for previews, and set every DRC check to at least *Warning* (VelaTrace refuses to route while any is set to *Ignore*; KiCad's defaults ignore five). Try it on a disposable copy first. If `kicad-cli` is not found on its own, enter its path in **Setup**.
- **Optional AI key.** The audit's checks need none. For **Explain with AI**, choose a provider and model in **Setup** and enter your key (kept in memory only). Gemini: protocol `gemini`, `https://generativelanguage.googleapis.com/v1beta`. Groq: protocol `openai`, `https://api.groq.com/openai/v1`.
- **Tested on Windows with KiCad 10.0.4 / 10.0.6 only.** Linux and macOS are untested.

Want to look around first? `pip install -e '.[dev]'` then `python -m velatrace --demo` runs the UI on synthetic data.

## Usage

**Audit.** Pick the open PCB (or a saved schematic/XML netlist) and click **Check design**. Built-in checks run on your computer with no key, no network and no dialogs, and list findings as errors, warnings, savings and info, e.g. `3 errors · 2 warnings`. Click a finding for its evidence, fix and cost. Checks include LEDs without a series resistor, I2C pull-up problems, duplicated ICs, unprotected power connectors and unset or 0 Ω values; the least reliable rules are Info-only ([details and limits](docs/behavior-and-limits.md#built-in-audit-checks)). Optional: **Explain with AI** sends the findings and design to your own provider for plain-language explanations and part roles, after a one-time privacy notice; each AI step shows its token and call cost beside the button, and clicking it is the consent. The hard call budget always applies. Pricing the flagged parts is a further opt-in click.

**Route.** Click **Route** at the top (Ctrl+2), add any rules (the list shown beside **Route board** is confirmed by the click), then click **Route board**. VelaTrace reads the open board (no save needed), checks it (outline, footprints outside it, duplicate references, existing copper), exports the DSN with KiCad's own bundled Python and routes it with Vela-routing, showing each stage with **Cancel routing**. If KiCad cannot export automatically, it offers **Load DSN exported from KiCad…** instead. The User.9 preview appears as soon as routing finishes; KiCad DRC then runs in the background and **Approve** unlocks when it passes. Then **Reject** (with a reason) or **Approve** (the click is the confirmation). If DRC findings block approval, **Approve anyway** applies the route after a confirmation that lists them; the override is recorded in the backup journal. Approval swaps the preview for real copper in one IPC commit, leaving the board unsaved so a single Undo reverts it. Currently supported: placed (saved at least once), initially unrouted boards with straight traces, through vias and one width/clearance for all nets. Anything else is refused.

Full behavior and limits: [docs/behavior-and-limits.md](docs/behavior-and-limits.md). Transaction safety: [docs/write-safety.md](docs/write-safety.md). UI guide: [docs/ui.md](docs/ui.md).

## Privacy

No backend, no telemetry. A notice names your provider and explains that component fields, connectivity and your description are sent with your key; it repeats whenever you change the endpoint or search settings. Avoid confidential/NDA designs unless you trust the provider. Note that Gemini's free tier may use prompts to improve its models; Groq's free tier does not make this claim. Provider policies, key handling and the full list of opt-in network use: [docs/privacy.md](docs/privacy.md).

## Contributing

Contributions, forks and bug reports are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md), and look for [good first issues](https://github.com/F-Blaze/VelaTrace/labels/good%20first%20issue). Tests need no key or running KiCad:

```sh
python -m pip install -e '.[dev]'
python -m ruff check src tests launch.py
python -m pytest
```

Security reports: see [SECURITY.md](SECURITY.md). VelaTrace is solo-maintained by F-Blaze with no response-time guarantee.

## License

VelaTrace is [MIT](LICENSE), including Vela-routing. **Route board** no longer runs Freerouting (GPLv3). The Freerouting adapter and its MIT launcher, `WarmRouter.java` (source in [src/velatrace/router_resources](src/velatrace/router_resources); the compiled `WarmRouter.class` is committed), remain in the repository for the research benchmarks only; they load a separately downloaded, unmodified Freerouting JAR. Whether that launcher and the JAR form a combined work is an open question the maintainer is reviewing; this README makes no legal claim either way.

The optional JLCPCB data files carry no licence statement, and the live lookup uses an undocumented JLCPCB website endpoint; see [docs/jlcpcb.md](docs/jlcpcb.md) and [docs/bom.md](docs/bom.md).
