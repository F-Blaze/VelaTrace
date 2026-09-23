# Contributing to VelaTrace

VelaTrace is maintained by F-Blaze without a support or response-time guarantee. Contributions and forks are welcome under the MIT license. Remote setup is currently pending, so the PR and release requirements below are gates to enable before publishing, not claims about an existing protected repository.

## Local development

Use Python 3.11+ and a virtual environment. From the repository root:

```sh
python -m pip install -e '.[dev]'
python -m ruff check src tests launch.py
python -m pytest
python -m velatrace --demo
```

Use `QT_QPA_PLATFORM=offscreen` on a headless host. Normal tests need no key or running editor. Native Freerouting and KiCad CLI tests skip unless explicitly configured; follow [testing.md](docs/testing.md) to run them. A skip is not a passed integration test. Never submit API keys, confidential boards, provider responses or private recovery backups as fixtures. Fixtures must be authored for this project or have an explicit compatible license and provenance.

Development checkouts may be used for local review and synthetic tests. Distribution and end-user installation require a verified signed release tag. Do not recommend installing from `main` or a feature branch.

## Changes and review

Create a focused branch and PR with the problem, changed behavior, relevant validation and remaining limitations. Every change to `main`, including maintainer work, requires independent review, passing required CI/CodeQL checks and resolved discussions. Do not bypass protection, directly push to `main`, self-approve or treat an agent's review as a GitHub approval. Bootstrap commits in this local repository still need independent PR review before release.

[CODEOWNERS](.github/CODEOWNERS) names the responsible owner and explicitly covers subprocesses, network transport, filesystem writes and installation surfaces. It only enforces review once GitHub requires code-owner approval. F-Blaze-authored PRs need a trusted second owner/reviewer with the necessary repository access; that person is not yet appointed. Add them before enabling merges, without inventing a username or disabling the requirement.

Changes involving shell/subprocess calls, network calls, writes outside the project folder or install scripts must explain the input trust boundary, allowed destinations, credential handling and failure behavior. Identify new such paths in CODEOWNERS. Runtime subprocesses must use argument arrays, avoid shells, sanitize environments and have deadlines. Provider calls must retain consent, endpoint restrictions, output/call caps and bounded retries. Any broader permission or destination needs an explicit design review.

## Implementation boundaries

- Use official `kicad-python` IPC on KiCad 9+; do not add SWIG/`pcbnew` fallbacks. Check capabilities against the actual server version.
- Treat all design fields, labels and model output as untrusted. Preserve the prompt boundary and plain-text UI. No model-generated commands, URLs, arithmetic or arbitrary tools.
- Confirm corrected component functions before classification, and confirm the actual token estimate before the run. Compute flags, totals, savings and tokens in code. A verdict never authorizes component deletion.
- Keep Freerouting as a pinned, unmodified, separate external process. Do not link or embed GPL code. The current repository does not bundle its JAR; any future distribution of the JAR must retain its GPLv3 license and satisfy applicable distribution obligations.
- Route only within confirmed constraints and existing stackup. Refuse unsupported or ambiguous SES in full. Never partially apply geometry or silently drop a constraint.
- Send every live board mutation through the backup/refusal/transaction boundary, including preview cleanup. Preserve foreign items, exact ownership, stale-state checks and uncertain-outcome blocking. Do not weaken comparisons merely to make a test pass.
- No backend or telemetry. Do not persist keys or silently expand provider-side processing. Update the notice version and documentation when disclosure meaning changes.

Tests should target observable behavior and meaningful failure cases. Include bulk/decoupling nonredundancy, duplicate sensors, malformed SES refusal, missing runtime startup failure and safety regressions when changing those areas. Add native evidence for changes at SDK/CLI/router boundaries. See [write-safety acceptance](docs/write-safety.md) before changing transaction semantics.

## Release preparation

Follow [repository security setup](docs/repository-security.md) and record evidence for every gate in [build status](docs/BUILD_STATUS.md). Scan the entire reachable history and release contents for secrets, review dependency changes, verify package license/probe contents and complete live editor/provider acceptance. Only create a signed annotated release tag after all changes have been merged through reviewed PRs. Publish the signing identity through a trusted channel; never fabricate a tag, fingerprint or successful check. A failed or pending gate blocks release.

Do not put security-sensitive details in public issues. [SECURITY.md](SECURITY.md) describes the currently pending private-reporting setup; no working private contact is claimed yet.
