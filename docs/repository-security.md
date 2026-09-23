# Repository security and release setup

Target: `F-Blaze/VelaTrace`. The human requested **remote setup pending**. These are instructions for the eventual repository, not enabled settings. No remote repository, reviewed PR history, protected `main` or signed release tag has been established by this build.

## Maintainer and review controls

1. **F-Blaze must enable 2FA** on the maintainer account and protect recovery credentials outside this repository.
2. Appoint a trusted second reviewer/code owner with suitable repository access. Add their real handle to CODEOWNERS. F-Blaze cannot approve a F-Blaze-authored PR; sole ownership must not become a bypass.
3. Protect `main` with PRs required for every actor, including the maintainer/admins, and no bypass. Require an independent approval and code-owner review, dismiss stale approvals, resolve conversations, and require passing CI and CodeQL checks on the current revision. Block force pushes and deletion. Select actual check names after the workflows have run.
4. Enable secret scanning and push protection; confirm availability for the repository/account and record the enabled settings. Enable private vulnerability reporting before advertising a private reporting channel. If any required protection is unavailable, record it as an unresolved release gate.
5. Ensure `.github/workflows/codeql.yml` runs on every PR, and require its successful result. Local lint or the presence of workflow YAML is not CodeQL execution evidence. Review workflow permissions and dependency updates independently.

CODEOWNERS covers all files and explicitly identifies current network, external process, live board, config/temp-file and install surfaces. A CODEOWNERS file alone does not require review; the branch rule must enforce it. New sensitive paths must be added during review. No install scripts currently run automatically beyond KiCad's standard dependency setup.

## Signed releases only

Before a release, independently review the bootstrap implementation through a PR and ensure every subsequent change reaches `main` through reviewed PRs. Preserve review/check evidence; local agent reviews cannot substitute for this requirement.

Run the full tests with native paths enabled and complete the live-editor/provider acceptance in [testing.md](testing.md). Review the [known capability limits](BUILD_STATUS.md), all board-write paths, privacy gating, package contents and external tool hashes. Scan all reachable Git history and the exact release tree for secrets. A clean scanner result is bounded evidence, not proof that no secret exists; inspect findings and exclusions.

Create an annotated tag signed with the approved maintainer key only after every gate passes. Verify its signature, publish the tag's commit and release artifact checksums, and make the trusted signing identity/fingerprint available independently. Do not invent a fingerprint here; none has been provisioned. Consumers must verify the signer as well as the signature. Never install or recommend installation from `main`.

Restrict release-tag creation/update to the approved release process and prevent replacement of published tags. GitHub's signed-commit rules are not a substitute for verifying the **tag object's** signature. Do not create an unsigned tag as a placeholder. PCM distribution remains deferred until stability and release gates are satisfied.

## Current local evidence

Phase 5 passed 101 tests and 58 subtests with no skips, including native Freerouting and the KiCad CLI fixture test. The wheel was inspected for its MIT license, independent probe hash and absence of a bundled router JAR or credential files. These checks do not validate live editor writes or remote settings.

The root review ran Gitleaks 8.30.1 across all ten local commits at the Phase 5 revision and found no matches. Its working-directory scan also found no matches in scanned files, but an untracked `.pytest_cache` directory was inaccessible and skipped. Repeat the scan against the final reviewed release revision and contents; this earlier result is neither an exhaustive secret guarantee nor GitHub secret-scanning/push-protection evidence.
