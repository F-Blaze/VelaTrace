"""Light/heavy split: hs.* checks run only through run_heavy."""
from velatrace.audit_rules import HEAVY_RULES, RULES, run_heavy, run_rules
from velatrace.copper import BoardCopper, Segment
from velatrace.findings import Finding, Severity
from velatrace.models import DesignSnapshot

LAYERS = ("F.Cu", "B.Cu")


def skewed():
    segments = (Segment("USB_D+", "F.Cu", (0.0, 0.0), (30.0, 0.0), 0.2, 30.0),
                Segment("USB_D-", "F.Cu", (0.0, 1.0), (5.0, 1.0), 0.2, 5.0))
    return DesignSnapshot((), "fixture", copper=BoardCopper(LAYERS, 1.6, segments, (), (), frozenset(), {}, {}))


def test_run_rules_has_no_heavy_findings():
    assert not set(HEAVY_RULES) & set(RULES)
    assert not [item for item in run_rules(skewed(), providers=()) if item.rule.startswith("hs.")]


def test_run_heavy_returns_info_findings():
    found = run_heavy(skewed())
    assert [item.rule for item in found] == ["hs.pair.skew"]
    assert all(item.severity == Severity.INFO for item in found)


def test_run_heavy_runs_providers_and_survives_a_failing_one():
    def fit(snapshot):
        return [Finding("fit.pad", Severity.WARNING, "Pad too small", ("U1",))]
    def broken(snapshot):
        raise RuntimeError("boom")
    found = run_heavy(skewed(), providers=[fit, broken])
    assert {item.rule for item in found} == {"hs.pair.skew", "fit.pad", "audit.check_failed"}
