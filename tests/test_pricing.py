"""Pricing consent, bounded search calls and code-only totals."""
from dataclasses import replace
from decimal import Decimal
import json
import unittest

from velatrace.audit import AuditSession
from velatrace.errors import ValidationError
from velatrace.models import DesignSnapshot
from velatrace.pricing import PricingSession
from velatrace.provider import Completion, ProviderError, Usage
from test_audit_review import FakeProvider, SENSORS


class QuoteProvider(FakeProvider):
    def __init__(self, quote=None, failure=None):
        super().__init__()
        self.config = replace(self.config, builtin_search=True, search_model='groq/compound')
        self.quote = quote
        self.failure = failure
        self.search_calls = []

    def complete(self, prompt, max_tokens, on_usage=None, **kwargs):
        if not kwargs.get('search'):
            return super().complete(prompt, max_tokens, on_usage, **kwargs)
        self.search_calls.append((prompt, max_tokens, kwargs))
        self.budget.consume()
        if self.failure:
            raise self.failure
        return Completion(json.dumps(self.quote), Usage(100, 10, 'fixture'))


def pricing_session(provider, mpn=None):
    parts = SENSORS if mpn is None else (SENSORS[0], replace(SENSORS[1], fields={'MPN': mpn}))
    audit = AuditSession(provider)
    audit.set_description('Temperature monitor')
    audit.load_design(DesignSnapshot(parts, 'fixture'))
    audit.infer_functions()
    audit.confirm_functions()
    estimate = audit.estimate_classification()
    audit.classify(estimate.confirmation_fingerprint)
    return PricingSession(audit)


class PricingTests(unittest.TestCase):
    def test_search_estimate_uses_actual_flagged_count_and_exact_prompt_bytes(self):
        provider = QuoteProvider({'unit_price_usd': '0.25', 'mpn': 'TMP123',
                                  'source': 'https://example.invalid/part'})
        session = pricing_session(provider, 'TMP123')
        estimate = session.prepare(300)
        self.assertEqual((estimate.flagged_count, estimate.searches_low, estimate.searches_high,
                          estimate.max_http_calls, estimate.output_cap_each), (1, 1, 2, 1, 300))
        prices = session.run(estimate.fingerprint)
        self.assertEqual(set(prices), {'U2'})
        prompt, cap, options = provider.search_calls[0]
        self.assertEqual(estimate.visible_prompt_token_upper_estimate,
                         len(json.dumps(prompt.messages(), ensure_ascii=False).encode('utf-8')))
        self.assertEqual((cap, options['timeout'], options['retries']), (300, 8, 0))
        self.assertFalse(prices['U2'].estimated)
        self.assertEqual(session.flagged_cost, Decimal('.25'))
        self.assertEqual(session.hypothetical_savings, Decimal('.25'))

    def test_timeout_uses_estimate_and_explains_failure(self):
        provider = QuoteProvider(failure=ProviderError('Provider search timed out.'))
        session = pricing_session(provider)
        estimate = session.prepare()
        result = session.run(estimate.fingerprint)
        self.assertEqual(len(provider.search_calls), 1)
        self.assertTrue(result['U2'].estimated)
        self.assertIn('estimate only — no part number found', result['U2'].note)
        self.assertIn('timed out', session.failure_summary)
        self.assertEqual(session.flagged_cost, Decimal('2.00'))

    def test_unconfirmed_or_changed_pricing_never_calls_search(self):
        provider = QuoteProvider()
        session = pricing_session(provider)
        estimate = session.prepare()
        with self.assertRaises(ValidationError):
            session.run('incorrect')
        provider.config = replace(provider.config, search_model='different-model')
        with self.assertRaises(ValidationError):
            session.run(estimate.fingerprint)
        self.assertEqual(provider.search_calls, [])

    def test_budget_shortfall_refuses_before_any_search(self):
        provider = QuoteProvider()
        session = pricing_session(provider)
        estimate = session.prepare()
        provider.budget.used = provider.budget.limit
        with self.assertRaisesRegex(ValidationError, 'hard API call cap'):
            session.run(estimate.fingerprint)
        self.assertEqual(provider.search_calls, [])

    def test_no_mpn_and_mismatched_mpn_remain_estimates(self):
        for mpn in (None, 'DIFFERENT'):
            with self.subTest(mpn=mpn):
                provider = QuoteProvider({'unit_price_usd': '0.25', 'mpn': 'TMP123',
                                          'source': 'https://example.invalid/part'})
                session = pricing_session(provider, mpn)
                result = session.run(session.prepare().fingerprint)
                self.assertTrue(result['U2'].estimated)
                self.assertEqual(result['U2'].mpn, mpn)

    def test_nonfinite_negative_or_unverifiable_prices_fall_back(self):
        for price, source in (('NaN', 'https://example.invalid/part'),
                              ('-1', 'https://example.invalid/part'),
                              ('1', 'https://user:password@example.invalid/part'),
                              ('1', 'http://example.invalid/part')):
            with self.subTest(price=price, source=source):
                session = pricing_session(QuoteProvider({'unit_price_usd': price, 'mpn': None,
                                                         'source': source}))
                result = session.run(session.prepare().fingerprint)
                self.assertTrue(result['U2'].estimated)
                self.assertEqual(result['U2'].amount, Decimal('2.00'))
                self.assertTrue(session.failure_summary)


if __name__ == '__main__':
    unittest.main()
