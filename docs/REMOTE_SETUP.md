# Remote setup status

Repository: [F-Blaze/VelaTrace](https://github.com/F-Blaze/VelaTrace), public.

The user authorized remote setup after the original local release review. `main` was initialized with the MIT license only, then protected before application code was uploaded. All implementation changes are submitted on `review/initial-alpha` for independent PR review. Public commits use the maintainer's GitHub noreply address. The original local development history remains on `bootstrap/foundation`; older local hashes in the build notes are provenance, not public commit links.

Verified repository settings:

- `main` requires a PR and one independent approval, code-owner review, dismissal of stale approvals, approval of the latest push, resolved conversations, and up-to-date required checks.
- Protection applies to administrators/maintainer. Force pushes and branch deletion are disabled. No PR bypass is configured.
- GitHub secret scanning and push protection are enabled.
- Private vulnerability reporting is enabled.
- An active tag ruleset blocks all tag creation, update and deletion without bypass until the trusted signing/release process is configured. No release tag exists.
- Required checks are currently `tests` and `analyze`; actual workflow results will be recorded here after the first PR run.

Still required:

1. F-Blaze confirms account 2FA; the API did not disclose its status.
2. Appoint a trusted second reviewer with repository write access and add them to CODEOWNERS before merging implementation. The author cannot approve their own PR.
3. Obtain independent approval and passing CI/CodeQL for the current PR revision. No merge is authorized by an agent review.
4. Complete the live KiCad/provider acceptance and remaining capabilities in [release review](RELEASE_REVIEW.md).
5. Provision a trusted signing identity and controlled signed-tag release process before changing the tag lock. Never publish or install from main.

The repository bootstrap is administrative license scaffolding, not a reviewed implementation merge or signed release.
