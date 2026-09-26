# Testing VelaTrace

Install the project and development dependencies in a virtual environment, then run from the repository root:

```sh
python -m pip install -e '.[dev]'
python -m ruff check src tests launch.py
python -m pytest
```

Linux CI installs `libegl1` and `libopengl0`, sets `QT_QPA_PLATFORM=offscreen`, verifies the pinned SDK and Qt imports, then runs the same checks. UI tests also select offscreen rendering locally. No API key, external provider calls, or running KiCad instance is needed for this suite. Ruff checks syntax errors, invalid comparisons and control flow, and undefined names; it does not enforce a broad formatting rewrite.

Phase 5 local evidence on Windows, 2026-09-22: **101 passed, 58 subtests passed**, with correctness lint passing and no skips. Both optional native tests (Freerouting and KiCad CLI) were enabled in that run. The default suite skips these two tests unless their executable paths are configured. The SDK geometry tests use real `kicad-python==0.8.0` wrappers; the UI tests use real `PySide6-Essentials==6.10.2` offscreen widgets. Mocked IPC/DRC tests do not certify a real KiCad transaction.

After the final audit review on 2026-09-23: **105 passed, 63 subtests passed, zero skips**, with both native integrations enabled. Added regressions bind classification consent to provider/model and token budget, and exclude uncertain duplicate/optional verdicts from savings. Correctness lint passed.

Release-review fixes on 2026-09-23: **107 passed, 66 subtests passed, zero skips**, plus correctness lint. Routing requires the exact editor/CLI version. Matching schematic context is exported successfully before explicit parity checking; the native CLI test also refuses a malformed matching schematic. A live divergent-schematic parity acceptance test remains required.

Coverage includes:

- Authored KiCad-format PCB, project and XML connectivity fixtures: bulk plus decoupling capacitors on one rail do not flag, identical nearby sensors do, and netlist-only ambiguity remains possible redundancy.
- Different values, footprints, types and pin mappings; missing fields, uncertain roles, proximity boundaries and conflicting critical/important verdicts; no removal based on a verdict.
- Decimal totals and hypothetical savings without double counting, invalid numeric input refusal, output caps, retry-inclusive token cost, actual prompt fingerprints and verified routing percentages.
- Pricing only the real flagged set, a separate consent fingerprint, built-in search timeout/retry arguments, hard call cap, explicit failure fallback, manufacturer part-number matching and untrusted quote handling.
- Malformed/unsupported SES refuses before candidate validation or board writing; missing Java/JAR fails at startup; router version/hash/probe checks; real SES unit conversion and placement binding.
- Real SDK construction of nanometer track/via geometry, through-via span, net mapping, dashed user-layer graphics and annotation objects, without a running editor.
- Backups before live mutation, backup failure refusal, partial-create rollback, failed removal, foreign-item preservation, stale rules/board refusal, uncertain IPC outcomes and blocked retries.
- Privacy consent persistence and endpoint binding, key-safe errors, bounded network deadlines, injection boundaries, plain-text UI labels and confirmation gates.

Enable the native external-process test only after installing the exact JAR and Java 21 runtime described in [freerouting.md](freerouting.md):

```powershell
$env:VELATRACE_TEST_JAR = 'C:\tools\freerouting-2.1.0.jar'
$env:VELATRACE_TEST_JAVA = 'C:\tools\java21\bin\java.exe'
python -m pytest tests/test_freerouting.py -k real_offline_router
```

The root integration review separately passed this native test on 2026-09-22: the production wrapper routed both default and constrained widths while its Java process policy refused network access. This does not exercise KiCad DRC or editor undo.

Enable the real KiCad CLI fixture test separately:

```powershell
$env:VELATRACE_TEST_KICAD_CLI = 'C:\Program Files\KiCad\10.0\bin\kicad-cli.exe'
python -m pytest tests/test_native_kicad.py
```

This native CLI regression passed on 2026-09-22 against the official Windows KiCad 10.0.6 executable. It uses a temporary copy of the authored PCB and project, an isolated KiCad configuration directory, and the production CLI adapter. It expects DRC violations and unconnected items on the deliberately unrouted fixture. On KiCad 10+, it also disables a check deliberately and confirms the adapter refuses incomplete DRC evidence. It does not connect to or mutate a running editor. The fixture uses the official `board.design_settings.rule_severities` schema; [KiCad source](https://docs.kicad.org/doxygen/board__design__settings_8cpp_source.html) documents this setting. The real test caught an incorrect setting name that mocked DRC could not detect.

Required before a signed release:

1. Open copies of the fixtures in supported KiCad versions and confirm actual IPC reading and saved schematic CLI export.
2. Validate a candidate with the real matching KiCad CLI/project/rules, inspect reported DRC and unconnected counts, and confirm nonzero counts prevent approval.
3. Exercise preview/reject cleanup and approve in the live editor; verify one Ctrl+Z reverts the entire approved routing, foreign items survive, and backup files recover the pre-write state.
4. Check KiCad 9 and newer compatibility, user-layer preview color/visibility, theme selection, and beside-editor window placement on each supported desktop OS.
5. Run provider integration with an explicitly consented nonconfidential fixture and the maintainer's key; verify exact input counts and actual streamed usage. No live-provider success is claimed by unit tests.
6. Run the full suite, native router test, package-content checks and reviewed GitHub security/release gates. A passing offline suite alone does not authorize a signed release.
