"""Versioned local disclosure acknowledgments; never stores keys or board data."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile

from .errors import ValidationError

NOTICE_VERSION = 1
PROVIDER_NOTE = ("Gemini's free tier may use your prompts to improve their models; "
                 "Groq's free tier does not make this claim.")


def consent_identity(config) -> str:
    # Bind the complete base path as well as the origin and protocol. Enabling
    # server-side search is an additional disclosure scope, even on one endpoint.
    payload = [NOTICE_VERSION, config.endpoint, config.protocol, config.builtin_search]
    return sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def disclosure_text(config) -> str:
    return (
        f"Board data is sent to {config.name} with your key.\nEndpoint: {config.endpoint}\n"
        f"Protocol: {config.protocol}\n\n"
        "This includes connectivity, component names and fields, your project description, "
        "and your corrections. Token-count requests can send the actual prompt too. "
        "Avoid confidential/NDA designs unless you trust that provider.\n\n"
        "VelaTrace has no backend or telemetry. Its remote API requests go only to your "
        "configured endpoint. KiCad IPC is local; the external router is run with network "
        "access denied. Provider retention and processing terms still apply.\n\n"
        + PROVIDER_NOTE + "\n\n"
        + ("Built-in web search is enabled: flagged component details can also be processed "
           "by the provider's search services. Groq search uses Tavily; Google Search "
           "grounding stores prompts, context and output for 30 days.\n\n"
           if config.search_available else config.search_unavailable_reason + "\n\n")
        + "Accept once for this endpoint, protocol and search setting? The acknowledgment "
        "file stores only a hash, never your key or design. Board backups are stored separately. See docs/privacy.md "
        "for policy links and how to reset this notice."
    )


class ConsentStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> set[str]:
        try:
            if not self.path.exists():
                return set()
            if self.path.stat().st_size > 100_000:
                raise ValueError()
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or set(value) != {"version", "acknowledgments"}
                    or type(value["version"]) is not int):
                raise ValueError()
            if value["version"] != NOTICE_VERSION:
                return set()
            entries = value["acknowledgments"]
            if (not isinstance(entries, list) or len(entries) > 500
                    or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)
                           for item in entries)):
                raise ValueError()
            return set(entries)
        except (OSError, ValueError, TypeError) as exc:
            raise ValidationError("Privacy acknowledgment file is unreadable or invalid. "
                                  "Reset privacy-consent.json in the local VelaTrace config "
                                  "folder and review the disclosure again.") from exc

    def accepted(self, config) -> bool:
        return consent_identity(config) in self._read()

    def accept(self, config):
        entries = self._read()
        entries.add(consent_identity(config))
        if len(entries) > 500:
            raise ValidationError("Too many saved privacy acknowledgments; reset the consent file.")
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".privacy-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump({"version": NOTICE_VERSION, "acknowledgments": sorted(entries)}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise ValidationError("Could not save the privacy acknowledgment. No analysis was "
                                  "started; check the local config folder permissions.") from exc
        finally:
            if temporary and temporary.exists():
                temporary.unlink(missing_ok=True)
