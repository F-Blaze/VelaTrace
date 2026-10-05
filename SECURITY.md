# Repository security

Upstream: [F-Blaze/VelaTrace](https://github.com/F-Blaze/VelaTrace). Main PR protection, secret scanning, push protection and private vulnerability reporting are enabled. The alpha has merged; each new PR still needs independent approval. A second code owner, broader live acceptance and release signing remain gates.

Before release, enable a main branch ruleset: PRs required for everyone including the maintainer, no bypass, approvals required, stale approvals dismissed, code owner review required, resolved discussions and passing CI/CodeQL required. Block branch deletion and force pushes. Enable secret scanning and push protection in repository settings.

Add a trusted second reviewer/code owner: a maintainer cannot approve their own PR, so @F-Blaze alone cannot satisfy code-owner approval on F-Blaze-authored changes. Do not bypass this requirement merely because there is one maintainer.

Alpha: install from a tagged release when available; cloning `main` is for testers. Once signed annotated release tags exist, verify the tag signature against an independently trusted maintainer key before installing. Local development commits are not evidence of reviewed PRs. First local commit contains the MIT license; no release tag is created until every release gate is independently satisfied.

Review subprocess/network code, install scripts, and all filesystem writes. Do not log API keys or design prompts. No automatic uploads, telemetry or backend. Use the repository's enabled private vulnerability reporting; no support guarantee.
