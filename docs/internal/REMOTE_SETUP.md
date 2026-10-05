# Remote setup status

Repository: [F-Blaze/VelaTrace](https://github.com/F-Blaze/VelaTrace), public.

The user authorized remote setup after the original local release review. `main` was initialized with the MIT license only, then protected before application code was uploaded. [PR #1](https://github.com/F-Blaze/VelaTrace/pull/1) merged on September 27, 2026 at 15:19 UTC as `3abfa346bf3394ff40508fb86224fe68de6b7cb4`, including the earlier PR #2 fixes. This review began from `main` revision `49e7278`, including subsequent PRs. Public commits use the maintainer's GitHub noreply address. Original local build hashes are provenance, not public commit links.

Repository settings rechecked September 27, 2026 (branch protection, secret scanning, push protection and absence of tags); other settings below retain their original setup verification:

- `main` requires a PR and one independent approval, code-owner review, dismissal of stale approvals, approval of the latest push, resolved conversations, and up-to-date required checks.
- Protection applies to administrators/maintainer. Force pushes and branch deletion are disabled. No PR bypass is configured.
- GitHub secret scanning and push protection are enabled.
- Private vulnerability reporting is enabled.
- An active tag ruleset blocks all tag creation, update and deletion without bypass until the trusted signing/release process is configured. No release tag exists.
- Required checks are `tests` and `analyze` from GitHub Actions, plus `CodeQL` from GitHub Advanced Security. Their producer app IDs are pinned in branch protection. Current results are visible on [PR #1 checks](https://github.com/F-Blaze/VelaTrace/pull/1/checks). The first CI run identified missing Linux Qt runtime libraries; the workflow installs `libegl1` and `libopengl0` before testing.

Still required:

1. Add the maintainer's trusted independent reviewer to CODEOWNERS. The checked file still lists only F-Blaze, who cannot approve their own PR.
2. Obtain independent approval and passing CI/CodeQL for the current PR revision. No merge is authorized by an agent review.
3. Complete the live KiCad/provider acceptance and remaining capabilities in [release review](RELEASE_REVIEW.md).
4. Provision a trusted signing identity and controlled signed-tag release process before changing the tag lock. Never publish or install from main.

The repository bootstrap is administrative license scaffolding, not a reviewed implementation merge or signed release.
