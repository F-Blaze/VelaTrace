# Privacy and provider setup

VelaTrace has no backend and no telemetry. At runtime its remote API requests go only to the HTTPS endpoint you configure, using your own API key. It connects to KiCad through local IPC. Freerouting runs as a separate process with network access denied by the verified Java policy. Provider-side search can contact the provider's search services; that processing is outside the plugin and follows their terms. Installation downloads are separate from runtime activity.

Before the first analysis, a notice identifies the provider and endpoint and explains that component fields, connectivity, your description and corrections are sent with your key. Gemini token-count requests also send the actual prompt. Avoid confidential/NDA designs unless you trust that provider. Declining prevents analysis; a changed endpoint, protocol, enabled-search setting or notice version requires fresh acceptance. The notice precedes inference, token counting, classification and pricing.

Keys can be entered in Setup or supplied through `VELATRACE_API_KEY`. VelaTrace keeps keys in process memory, hides them in settings representations, and does not write them to its configuration, router arguments or logs. Keys inherited from the environment are removed from the local-tool child environments. Your shell or operating system may retain environment configuration you create independently.

## Choosing a provider

Groq or Gemini offer free tiers suitable for trying VelaTrace, subject to available models, quotas, region and account terms. Choose an exact currently available model; free access and search availability are not guaranteed. [Groq billing FAQ](https://console.groq.com/docs/billing-faqs), [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing).

Gemini's free tier may use your prompts to improve their models; Groq's free tier does not make this claim.

Policy review: **2026-09-22**. Google's unpaid-service terms allow product/model improvement and human review of inputs and outputs. API access through a project with active billing receives paid-service data terms; those exclude product improvement from prompts/responses, but other retention still applies. Google Search grounding separately retains prompts, context and output for 30 days for its operation, debugging and testing. Read the applicable account and regional terms before sending a design. [Gemini API terms](https://ai.google.dev/gemini-api/terms).

Groq documents default non-retention of inference inputs/outputs, with exceptions for reliability and abuse investigations that can retain data for up to 30 days (or longer if legally required). Usage metadata is retained. Its data controls offer zero data retention; do not confuse the plugin's lack of telemetry with the provider collecting none. [Groq data policy](https://console.groq.com/docs/your-data).

Groq's built-in web search uses Tavily, so enabling search adds provider-side processing beyond ordinary inference. [Groq web search](https://console.groq.com/docs/compound/built-in-tools/web-search). Groq's Compound Mini page records its decommissioning on September 21, 2026; do not configure that retired model. [Compound Mini status](https://console.groq.com/docs/compound/systems/compound-mini).

## Gemini pricing limitation

Gemini analysis and exact token counting are supported, but **Gemini web search is disabled in this build**. Google's grounding terms require associated Search Suggestions with grounded results, and its documentation describes the supplied display metadata. VelaTrace's structured pricing cards do not yet implement that presentation. Even if search is requested in configuration, pricing estimates report zero searches and zero API calls and use local illustrative amounts. Direct Gemini search/count requests are refused before network access. No provider HTML or remote assets are rendered. [Google grounding documentation](https://ai.google.dev/gemini-api/docs/google-search), [grounding terms](https://ai.google.dev/gemini-api/terms#grounding-with-google-search).

## Local files and resetting consent

The local config folder shown in Setup (the platform's Qt `AppConfigLocation`) contains `privacy-consent.json` and universal constraints. Consent stores only the notice version and endpoint/protocol/search acknowledgment hashes, never keys, prompts or board data. Delete `privacy-consent.json` to require disclosure again; an unreadable or malformed file blocks analysis with a visible error. Config writes are atomic.

Board data **is** saved locally for write safety. Before every live board mutation, `.velatrace/backups` beside the board holds saved/live recovery copies and transaction journals. These are retained for recovery and may contain confidential design information. Candidate board files use temporary directories under that backups folder. KiCad CLI DRC JSON reports use the operating system temporary folder. Router DSN/SES work files use temporary directories under the application config folder's `router-work`; normal completion cleans them up. A crash may leave temporary files. Router output is bounded and retained only in memory during the launch. Inspect and remove obsolete backups or abandoned temporary directories yourself after verifying recovery is no longer needed; VelaTrace never uploads them automatically.

Missing/invalid keys, unavailable providers and exhausted quotas produce visible errors. A failed pricing search uses a clearly marked local estimate and displays its failure count and sanitized reason above the cards. No live provider call or real API key was used to test these privacy boundaries.
