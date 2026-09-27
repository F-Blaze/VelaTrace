# Repository security

Upstream: [F-Blaze/VelaTrace](https://github.com/F-Blaze/VelaTrace). Main PR protection, secret scanning, push protection and private vulnerability reporting are enabled. See [verified remote settings](docs/REMOTE_SETUP.md). Independent approval, a second code owner, live acceptance and release signing remain pending.

Before release, enable a main branch ruleset: PRs required for everyone including the maintainer, no bypass, approvals required, stale approvals dismissed, code owner review required, resolved discussions and passing CI/CodeQL required. Block branch deletion and force pushes. Enable secret scanning and push protection in repository settings. Enable 2FA on the maintainer account.

Add a trusted second reviewer/code owner: a maintainer cannot approve their own PR, so @F-Blaze alone cannot satisfy code-owner approval on F-Blaze-authored changes. Do not bypass this requirement merely because there is one maintainer.

Only signed annotated release tags may be used for installs. Verify the tag signature against an independently trusted maintainer key before installing. Main is for development and must never be the install target. Local development commits are not evidence of reviewed PRs. First local commit contains the MIT license; no release tag is created until every release gate is independently satisfied.

Review subprocess/network code, install scripts, and all filesystem writes. Do not log API keys or design prompts. No automatic uploads, telemetry or backend. Use the repository's enabled private vulnerability reporting; no support guarantee.
