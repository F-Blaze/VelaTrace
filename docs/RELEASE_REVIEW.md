# VelaTrace release review

Reviewed on 2026-09-23 for the local `bootstrap/foundation` development branch. Target upstream: `F-Blaze/VelaTrace`. **Release is blocked.** This review completes the requested release-review agent pass; it does not certify production readiness, authorize publication, or replace independent human PR review.

Remote setup was subsequently resumed by the user. The historical gate results below describe the original local review; [remote setup status](REMOTE_SETUP.md) records the current repository protections and workflow evidence. Release remains blocked by independent approval, live acceptance, unfinished capabilities and signing.

## Requested release gates

| Gate | Result | Evidence and limit |
| --- | --- | --- |
| All changes merged through reviewed PRs | **Pending** | No remote, protected `main`, or reviewed GitHub PR history exists. Local commits and agent reviews do not satisfy this gate. Remote setup remains pending by the user's explicit choice. |
| No secrets in history | **Passed bounded local scan; remote controls pending** | Root ran Gitleaks 8.30.1 with `--all` across 13 commits through `11e9d65` (381,753 bytes), and against a clean `git archive` tree (371,224 bytes). Both found no matches; the archive scan had no inaccessible cache directories. This is scanner evidence, not proof that no secret can exist. Repeat scans after subsequent source changes and before publication. GitHub secret scanning and push protection remain unconfigured. |
| Signed release tag | **Pending** | No tag, approved signing identity, or trusted signing fingerprint exists. Do not create an unsigned placeholder or install from `main`. |
| Backup and refusal before every live board write | **Passed code/failure-test review; live acceptance pending** | All production `create_items`/`remove_items` calls are centralized in `BoardSafety._mutate`. Saved/live backups, snapshot checks, durable intent, exact-ID ownership checks, and a single IPC transaction precede or surround additions/removals. Preview creation and cleanup use the same boundary. Backup failure refuses mutation; partial operations drop the commit; uncertain transaction or journal outcomes block retry. Actual editor SaveCopy/rollback/Undo behavior remains unverified. |
| Privacy notice before first analysis | **Passed local implementation/tests; provider acceptance pending** | UI displays endpoint-specific disclosure; both generation and token-count requests call the provider authorization gate. Acknowledgments are versioned and bound to endpoint, protocol and search setting. No live provider request or real key was used in acceptance testing. |
| Phase 5 required tests | **Passed local suite** | Final completed root run including the review fixes: **107 tests and 66 subtests passed, zero skips**, with correctness lint and whitespace checks passing. Includes real pinned SDK, offscreen Qt, external Freerouting 2.1.0/Java 21 and official KiCad 10.0.6 CLI fixture tests. Required bulk/decoupling, duplicate sensor, malformed SES and missing Java/router cases are covered. |

## Findings resolved during review

1. **Schematic parity was not explicitly enabled.** `KiCadCli.drc` originally consumed a `schematic_parity` report field without passing the opt-in `--schematic-parity` switch. The official local KiCad 10.0.6 CLI help confirms that this flag enables the check. The corrected adapter exports and parses the matching saved schematic first, refusing unusable context, then passes the parity flag. PCB-only validation remains available when no schematic exists. Command-construction/failure regressions pass; the real CLI also refuses a deliberately malformed matching schematic. A complete real candidate with a valid matching schematic remains part of live acceptance.
2. **Candidate validation did not require the connected editor version.** The original routing setup accepted any CLI major version at least 9. The corrected setup calls `require_editor_version` before creating its safety/validator/session/export-ticket state and requires the exact major/minor/patch tuple. Regressions cover a matching version and mismatched major and patch versions.

Both changes and their focused regressions were re-reviewed. No additional material defect was found in this bounded pass. Passing these local checks does not remove the separate release blockers below.

## Scope and safety observations

Strict SES parsing rejects unknown sections, unsupported geometry, unverified via mappings and changed footprint placements before candidate construction or live writes. Candidate evidence binds the DSN, route geometry, saved board and project/rule context; approval requires zero verified DRC violations and unconnected items. Scope changes invalidate constraint confirmation. Unsupported header/per-net constraints stop explicitly, and blank rejection reasons prevent a retry.

Audit classification cannot run until functions and the actual prompt token budget are confirmed. Board-sourced data is escaped inside explicit untrusted-data delimiters. Code computes flags, Decimal totals, savings and token estimates; low-confidence flags do not contribute hypothetical savings. The audit does not delete or edit parts. Pricing targets only the flagged set, has separate consent, bounded search calls and visible fallback errors.

The network adapter accepts a configured HTTPS endpoint, rejects redirects through non-success status handling, bounds DNS/request time, limits calls/retries and requires disclosure. External Freerouting runs separately under its verified Java policy with the pinned unmodified JAR; no GPL router code/JAR is bundled. The Java policy is not an operating-system sandbox for hostile native code, and the official JAR/runtime remain trust anchors.

Root inspected the development wheel built from `11e9d65`: MIT license, runtime module/source agreement, independent probe SHA-256 and no bundled JAR or credential files. That unsigned scratch wheel is packaging evidence, not a release artifact. Local Markdown links passed review. The final scratch wheel is 69,543 bytes, SHA-256 `d05d9307f046f948723e8126e49336b7d6dacf327a846ffa5454014fabe02889`; all packaged Python modules match the reviewed source.

## Remaining release blockers

- The broader requested capabilities remain incomplete: manual fresh DSN export; initially unrouted boards only; straight tracks/through vias; all-net width/clearance only; no header keepout or per-net routing enforcement; hierarchical schematic context refused. Gemini search pricing remains disabled pending required grounding presentation.
- Native CLI testing proves the authored fixture loads and incomplete DRC reports are refused. It does **not** prove a real routed candidate is complete and DRC-clean under its matching project/rules.
- Test live IPC preview/reject cleanup, backups, exact server-returned geometry, in-commit reads, dropped/disconnected transactions and one Ctrl+Z for an approved route in every supported editor version. No such live test has passed yet. Automatic crash reconciliation is absent; journals support manual inspection/recovery.
- Confirm exact token counting and streamed usage against a consented nonconfidential provider test. Cross-version and non-Windows UI/editor behavior also need acceptance evidence.
- Before remote release, F-Blaze must enable 2FA, appoint a trusted independent code owner, require PRs with no maintainer bypass, enforce CI/CodeQL and code-owner review, and enable secret scanning/push protection. A sole author cannot approve their own PR. Workflow files do not enable these settings or prove CodeQL ran.
- Independently review the bootstrap through a PR, rescan the final revision/tree, verify packaging, then create and verify a signed annotated release tag with an independently trusted signer. Keep PCM deferred until stable.

The README and [build status](BUILD_STATUS.md) clearly identify this as a local development alpha and disclose these limits. Preserve that wording until the corresponding acceptance and release gates have actually passed.
