# Repository security setup (pending)

Intended upstream: F-Blaze/VelaTrace. No remote repository settings are asserted by this local checkout. The human requested leaving remote setup pending.

Before release, enable a main branch ruleset: PRs required for everyone including the maintainer, no bypass, approvals required, stale approvals dismissed, code owner review required, resolved discussions and passing CI/CodeQL required. Block branch deletion and force pushes. Enable secret scanning and push protection in repository settings. Enable 2FA on the maintainer account.

Add a trusted second reviewer/code owner: a maintainer cannot approve their own PR, so @F-Blaze alone cannot satisfy code-owner approval on F-Blaze-authored changes. Do not bypass this requirement merely because there is one maintainer.

Only signed annotated release tags may be used for installs. Verify the tag signature against an independently trusted maintainer key before installing. Main is for development and must never be the install target. Local development commits are not evidence of reviewed PRs. First local commit contains the MIT license; no release tag is created until every release gate is independently satisfied.

Review subprocess/network code, install scripts, and all filesystem writes. Do not log API keys or design prompts. No automatic uploads, telemetry or backend. Use private vulnerability reports once the upstream repository's security reporting is configured; no support guarantee.
