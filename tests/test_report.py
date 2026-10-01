from datetime import datetime
from decimal import Decimal

from velatrace.audit_rules import summarize
from velatrace.findings import Finding, Severity
from velatrace.models import Component, DesignSnapshot
from velatrace.report import render_report, save_report

WHEN = datetime(2026, 10, 1, 12, 30)
EVIL = "<script>alert(1)</script>"


def comp(ref="U1", value="MCU", fp="Package_QFP:LQFP-32"):
    return Component(reference=ref, value=value, footprint=fp)


def sample():
    return [
        Finding("a", Severity.ERROR, "U1 missing decoupling", ("U1",), ("+3V3",), "no cap",
                "add 100nF"),
        Finding("b", Severity.WARNING, "Odd pull-up", ("R1",)),
        Finding("c", Severity.SAVING, "Use 0603", ("C1",), cost_delta=Decimal("-0.25")),
        Finding("d", Severity.SAVING, "Swap part", ("C2",), cost_delta=Decimal("-0.17")),
        Finding("e", Severity.INFO, "FYI", ()),
    ]


def render(findings, *parts, **kw):
    snapshot = DesignSnapshot(components=tuple(parts or (comp(),)), source="test")
    return render_report(snapshot, findings, generated_at=WHEN, **kw)


def test_every_severity_and_sections():
    html = render(sample(), project_name="Demo", version="1.2")
    for text in ("Errors (1)", "Warnings (1)", "Savings (2)", "Info (1)", "U1 missing decoupling",
                 "add 100nF", "+3V3", "no cap", "-$0.25/board", "Demo", "2026-10-01 12:30",
                 "1 parts", "VelaTrace 1.2"):
        assert text in html
    assert html.index("Errors (1)") < html.index("Warnings (1)") < html.index("Savings (2)")


def test_summary_matches_summarize_and_cost_total():
    findings = sample()
    html = render(findings)
    assert summarize(findings) in html
    assert "est. $0.42/board saving" in html


def test_empty_findings():
    html = render([])
    assert "No issues found" in html
    assert "<h2>Errors" not in html


def test_escaping_of_board_strings():
    finding = Finding("x", Severity.ERROR, EVIL, (EVIL,), (EVIL,), EVIL, EVIL)
    html = render([finding], comp(ref=EVIL, value=EVIL, fp=EVIL), project_name=EVIL)
    assert "<script" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_deterministic_self_contained_sorted_bom():
    a = render(sample(), comp("B1"), comp("A1"))
    assert a == render(sample(), comp("B1"), comp("A1"))
    assert a.index("<td>A1</td>") < a.index("<td>B1</td>")
    assert "<script" not in a and "src=" not in a and "http://" not in a
    assert "https://github.com/F-Blaze/VelaTrace" in a and "<details>" in a


def test_save_report_utf8_atomic(tmp_path):
    target = tmp_path / "r.html"
    target.write_text("old", encoding="utf-8")
    save_report(target, "café — ok")
    assert target.read_bytes().decode("utf-8") == "café — ok"
    assert [p.name for p in tmp_path.iterdir()] == ["r.html"]
