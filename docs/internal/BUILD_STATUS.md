# VelaTrace build status

Current status, 2026-09-27: **development alpha, no signed release**. PR #1 was merged into protected `main` on September 27; the preview-approval fix is being reviewed separately. The [current review](REVIEW_2026_09_27.md) supersedes the historical evidence below for tests, live IPC and fixes. The original local `bootstrap/foundation` history is retained. See [remote setup](REMOTE_SETUP.md).

The phase evidence and verification counts below record the original September 23 build. They are not claims about the latest revision.

## Phase evidence

- **Phase 0 — foundation:** architecture feasibility reviewed before UI, official IPC only, MIT in first commit `1157429`, packaging and security workflow scaffolding.
- **Phase 1 — audit:** real connectivity, required description, editable function confirmation, prompt-bound token consent, code-side flags/math, flagged-only pricing, call limits and injection boundary. Audit and injection-guard reviews completed. The resumed audit review corrected low-confidence savings and bound classification consent to provider/model and the token budget.
- **Phase 2 — routing:** numeric scoped constraints, fresh DSN handoff, strict SES parsing, pinned external offline Freerouting, candidate validation and backed-up single-commit IPC writer. Routing, Freerouting and write-safety agents completed; live editor acceptance remains open.
- **Phase 3 — UI:** external Audit/Routing window, component cards, constraints, worker-thread services, token/pricing dialogs, preview/annotation controls and IPC launcher. Qt offscreen review completed.
- **Phase 4 — privacy:** endpoint-bound first-use notice, key-memory handling, explicit provider errors and pricing fallback disclosure reviewed. Gemini search is disabled pending grounding presentation; ordinary analysis is supported.
- **Phase 5 — tests:** reviewed and committed as `dc7fc35`; **101 tests passed, 58 subtests passed, zero skips**, with correctness lint passing on 2026-09-22. Both optional native tests were enabled.
- **Phase 6 — docs:** README install/setup/limitations, CONTRIBUTING, explicit CODEOWNERS paths and security checklist completed and reviewed by root. Local documentation links and whitespace checks pass. The independent release-review marker is complete; see [release review](RELEASE_REVIEW.md). Its two findings were fixed and re-reviewed. Local review is complete, but release remains blocked.

Final regressions on 2026-09-23: **107 tests passed, 66 subtests passed, zero skips**, including both native integrations. Correctness lint passed.

## What was verified

The suite used real `kicad-python==0.8.0` object wrappers and `PySide6-Essentials==6.10.2` offscreen widgets. Authored PCB/project/XML fixtures test bulk-versus-decoupling nonredundancy, nearby duplicate sensors and real pin/net connectivity. Failure injection covers backup refusal, partial-create rollback, exact ownership, changed rules and uncertain transaction blocking.

The official Freerouting 2.1.0 JAR ran separately under Temurin Java 21.0.12.1+1. Default and constrained routing produced valid SES; both the policy probe and actual blocked update request confirmed network denial. No GPL router binary is bundled.

An official, signed Windows KiCad 10.0.6 installer was extracted as data into an isolated workspace runtime; KiCad was not installed system-wide. Its real CLI parsed the authored PCB/project and reported expected DRC violations and unrouted connections. Production parsing also refused a deliberately incomplete report with disabled checks. This is fixture/CLI evidence, **not a passed DRC report for a routed candidate** and not a live IPC test.

The wheel was checked for the MIT license, required runtime modules, independent probe hash and absence of a bundled router JAR or credential files. Root review's Gitleaks 8.30.1 scan found no matches across all 13 commits through final source revision `11e9d65` and its clean archived source tree, without the earlier untracked-cache exclusion. See [security evidence limits](repository-security.md). No provider API key or live provider request was used.

## Open capability and release gates

| Gate | Current state |
| --- | --- |
| Fully automatic DSN export | Fresh manual KiCad DSN export required on the supported 9/10 baseline. |
| General routing support | Only initially unrouted saved boards, straight tracks, through vias and all-net width/clearance. Existing routing, hierarchical schematic context and header/per-net constraints refuse. |
| Docking and native transient overlays | External window and guarded User.9 graphics. KiCad controls layer color; per-layer dashed violet is available in the panel. |
| Live candidate validation | Disposable two-pad route passed matching KiCad 10.0.4 candidate DRC; broader geometry/version coverage remains open. |
| Live IPC writes and Undo | Preview, unsaved approval, reads in a commit, cleanup and one Undo passed KiCad 10.0.4. Live backups use get_as_string, not SaveCopy. Crash/disconnection coverage remains limited. |
| Privacy/provider integration | Notice and errors tested locally; exact live model token counts/streaming need consented nonconfidential provider integration. Gemini search display remains unimplemented. |
| Cross-version/OS behavior | No blanket KiCad 9/10/11 or desktop OS certification. Windows CLI and offline SDK/Qt evidence are narrower. |
| Reviewed PRs and protected main | Protected main contains the merged alpha. Each new fix still needs independent review; CODEOWNERS currently lists only the author. |
| Secret scanning/push protection and CodeQL | Secret scanning and push protection are enabled. PR workflow results are recorded in [remote setup](REMOTE_SETUP.md). |
| Signed release | No signing identity/fingerprint, signed tag or production install target exists. All prior gates must pass first. |

Backups and refusal are implemented for every live board-write path, including temporary graphics. Automatic crash reconciliation is not implemented; exact-ID journals support manual recovery. Do not save a board containing temporary graphics. Inspect an uncertain state before retrying.

## Reproduce and resume

Follow [testing.md](../testing.md) for environment setup and optional native paths. Default tests skip native router/CLI checks unless their executables are configured. [CONTRIBUTING](../../CONTRIBUTING.md) and [repository security setup](repository-security.md) describe independent review and release controls. Development tooling under the workspace `work/` folder is not part of the deliverable or an installation dependency.

Do not call this production-ready, bypass review, install from main, create an unsigned release, or replace an unsupported operation with a success claim.
