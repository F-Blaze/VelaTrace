"""Consent persistence and the network boundary; no real provider requests."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from velatrace.errors import CapabilityError, ValidationError
from velatrace.privacy import ConsentStore, NOTICE_VERSION, disclosure_text
from velatrace.provider import Provider, ProviderConfig, ProviderError
from velatrace.tokens import Prompt


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config" / "privacy-consent.json"
        self.store = ConsentStore(self.path)
        self.config = ProviderConfig("Test", "https://example.invalid/api", "fixture", "secret-fixture-key")

    def tearDown(self):
        self.temp.cleanup()

    def test_durable_scope_has_no_key_or_board_data(self):
        self.assertFalse(self.store.accepted(self.config))
        self.store.accept(self.config)
        saved = self.path.read_text()
        self.assertNotIn(self.config.api_key, saved)
        self.assertNotIn(self.config.endpoint, saved)
        self.assertEqual(set(json.loads(saved)), {"version", "acknowledgments"})
        second = ConsentStore(self.path)
        self.assertTrue(second.accepted(replace(self.config, api_key="new-secret", model="next")))
        for changed in (replace(self.config, endpoint="https://elsewhere.invalid/api"),
                        replace(self.config, endpoint="https://example.invalid/other"),
                        replace(self.config, protocol="openai"),
                        replace(self.config, builtin_search=True)):
            self.assertFalse(second.accepted(changed))
        self.path.unlink()
        self.assertFalse(second.accepted(self.config))

    def test_old_notice_and_malformed_file_never_grant_consent(self):
        self.store.accept(self.config)
        content = json.loads(self.path.read_text())
        content["version"] = NOTICE_VERSION - 1
        self.path.write_text(json.dumps(content))
        self.assertFalse(self.store.accepted(self.config))
        self.path.write_text('{"version":1,"acknowledgments":[true]}')
        with self.assertRaises(ValidationError):
            self.store.accepted(self.config)

    def test_atomic_write_failure_does_not_record_acceptance(self):
        with patch("velatrace.privacy.os.replace", side_effect=OSError("write failed")):
            with self.assertRaisesRegex(ValidationError, "No analysis was started"):
                self.store.accept(self.config)
        self.assertFalse(self.store.accepted(self.config))
        self.assertEqual(list(self.path.parent.glob(".privacy-*")), [])

    def test_no_consent_blocks_inference_count_and_search_before_network(self):
        config = replace(self.config, protocol="openai", search_model="groq/compound", builtin_search=True)
        provider = Provider(config, disclosure_gate=self.store.accepted)
        with patch("velatrace.provider.http.client.HTTPSConnection") as transport:
            for operation in (lambda: provider.count_tokens(Prompt("system", "board data")),
                              lambda: provider.complete(Prompt("system", "board data"), 50),
                              lambda: provider.complete(Prompt("system", "part data"), 50, search=True)):
                with self.assertRaisesRegex(ProviderError, "first-use"):
                    operation()
            transport.assert_not_called()
        self.assertEqual(provider.budget.used, 0)

    def test_missing_key_and_disclosure_are_explicit_without_exposing_key(self):
        self.assertNotIn(self.config.api_key, repr(self.config))
        self.assertNotIn(self.config.api_key, disclosure_text(self.config))
        self.assertIn("Avoid confidential/NDA", disclosure_text(self.config))
        self.assertIn("Tavily", disclosure_text(replace(self.config, protocol="openai", builtin_search=True)))
        provider = Provider(replace(self.config, api_key=""), disclosure_gate=lambda _: True)
        with self.assertRaisesRegex(ProviderError, "Missing API key"):
            provider.count_tokens(Prompt("system", "data"))

    def test_http_errors_are_actionable_and_do_not_read_provider_error_bodies(self):
        self.store.accept(self.config)
        provider = Provider(self.config, disclosure_gate=self.store.accepted)
        for status, message in ((401, "API key or permission"), (503, "outage"),
                                (429, "rate limit"), (302, "HTTP 302")):
            connection = MagicMock()
            connection.sock = None
            response = connection.getresponse.return_value
            response.status = status
            response.read1.return_value = self.config.api_key.encode()
            with patch("velatrace.provider.http.client.HTTPSConnection", return_value=connection):
                with self.assertRaisesRegex(ProviderError, message) as error:
                    provider._request("/fixture", {}, 1)
            response.read1.assert_not_called()
            response.close.assert_called_once()
            connection.close.assert_called_once()
            self.assertNotIn(self.config.api_key, str(error.exception))

    def test_gemini_search_refused_but_regular_analysis_body_is_available(self):
        config = replace(self.config, builtin_search=True)
        self.store.accept(config)
        provider = Provider(config, disclosure_gate=self.store.accepted)
        prompt = Prompt("system", "data")
        with patch("velatrace.provider.http.client.HTTPSConnection") as transport:
            with self.assertRaisesRegex(CapabilityError, "Gemini web search is disabled"):
                provider.complete(prompt, 50, search=True)
            with self.assertRaisesRegex(CapabilityError, "Gemini web search is disabled"):
                provider.count_tokens(prompt, 50, search=True)
            transport.assert_not_called()
        body = provider._gemini_body(prompt, 50, config.model)
        self.assertNotIn("tools", body)
        self.assertEqual(body["generationConfig"]["maxOutputTokens"], 50)
        self.assertEqual(provider.budget.used, 0)

    def test_gemini_pricing_preflight_and_run_make_zero_calls(self):
        from velatrace.audit import AuditSession, AuditStage
        from velatrace.flags import Flag
        from velatrace.models import Component, DesignSnapshot, Pin
        from velatrace.pricing import PricingSession
        config = replace(self.config, builtin_search=True)
        audit = AuditSession(Provider(config, disclosure_gate=self.store.accepted))
        audit.set_description("Fixture monitor")
        audit.load_design(DesignSnapshot((Component("R1", "10k", "R:0603",
            (Pin("1", "VCC"), Pin("2", "GND"))),), "fixture"))
        audit.stage = AuditStage.CLASSIFIED
        audit.flags = [Flag("R1", "Review")]
        pricing = PricingSession(audit)
        estimate = pricing.prepare()
        self.assertEqual((estimate.searches_low, estimate.searches_high, estimate.max_http_calls,
                          estimate.visible_prompt_token_upper_estimate), (0, 0, 0, 0))
        self.assertIn("Gemini web search is disabled", estimate.note)
        with patch.object(audit.provider, "complete") as request:
            results = pricing.run(estimate.fingerprint)
            request.assert_not_called()
        self.assertTrue(results["R1"].estimated)
        self.assertIn("zero API searches", results["R1"].note)


if __name__ == "__main__":
    unittest.main()
