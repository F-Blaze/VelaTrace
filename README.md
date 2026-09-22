# VelaTrace

A local KiCad 9+ IPC companion for component audits and guarded external routing. This repository is under active development; review the [release gates](docs/BUILD_STATUS.md) before installing. Maintainer: F-Blaze. License: MIT.

## Privacy

Bring your own API key. VelaTrace has no backend and no telemetry: its remote API requests go only to your configured endpoint. The first-use notice identifies that provider before sending board data. Avoid confidential/NDA designs unless you trust the provider. Keys stay in memory; consent records contain only hashes. Safety backups retain board data locally.

Groq or Gemini offer free tiers subject to their current terms and quotas. Gemini's free tier may use your prompts to improve their models; Groq's free tier does not make this claim.

Read [privacy and provider setup](docs/privacy.md) for verified policy links, provider-side web-search processing, local storage and consent reset instructions. [Groq data policy](https://console.groq.com/docs/your-data), [Gemini API terms](https://ai.google.dev/gemini-api/terms).

Gemini analysis is supported; Gemini web-search pricing is disabled until its required grounding display is implemented. Gemini pricing currently uses local estimates with zero search calls.
