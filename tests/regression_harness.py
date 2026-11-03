"""Shared by tests/test_regression_boards.py and tools/update_regression.py: run the light audit on the
regression boards and score the findings against the committed answer keys.

Matching is a compact port of the bench's score.py: same rule (or rule family) and overlapping refs/nets.
"""
import json
from dataclasses import replace
from pathlib import Path

from velatrace.audit_rules import run_rules
from velatrace.bom import bom_findings
from velatrace.netlist import read_xml_netlist

DIR = Path(__file__).resolve().parent / "regression"
EXPECTED, FLOORS = DIR / "expected.json", DIR / "floors.json"
META = ("audit.coverage", "audit.check_failed", "audit.crash")


def slugs():
    return sorted(p.name for p in (DIR / "boards").iterdir() if (p / "netlist.xml").exists())


def family(rule):
    if rule in ("value_merge", "value_normalise", "package_merge", "jlc_assign"):
        rule = "bom." + rule
    if rule == "value.zero_ohm":
        rule = "value.zero_ohm_short"
    if rule == "net.label_scope":
        rule = "net.single_pin"
    for p in ("i2c.pullup.", "decoupling.", "bom.jlc_"):
        if rule.startswith(p):
            return p
    return rule


def _nets(nets):
    return {n.rsplit("/", 1)[-1].strip().upper() for n in nets}


def same(f, g):
    """f: finding, g: answer-key entry."""
    if family(f["rule"]) != family(g["rule"]):
        return False
    fr, gr, fn, gn = set(f["refs"]), set(g.get("refs", [])), _nets(f["nets"]), _nets(g.get("nets", []))
    if not (gr or gn):
        return True  # blanket entry for that rule on that board
    if fr and gr:
        return bool(fr & gr) and (not (fn and gn) or bool(fn & gn))
    return bool(fn and gn and fn & gn)


def key(f):
    """Identity of a finding: rule + refs (+ nets when it has no refs)."""
    return f["rule"], tuple(sorted(f["refs"])), () if f["refs"] else tuple(sorted(f["nets"]))


def run_board(slug):
    snap = read_xml_netlist(DIR / "boards" / slug / "netlist.xml")
    snap = replace(snap, components=tuple(c for c in snap.components if c.nets))  # as the UI does
    found = run_rules(snap, providers=[lambda s: bom_findings(s, None)])
    return [dict(rule=f.rule, refs=list(f.refs), nets=list(f.nets)) for f in found if f.rule not in META]


def answer_key(slug):
    gt = json.loads((DIR / "answers" / f"{slug}.json").read_text(encoding="utf-8"))
    for name in ("issues", "acceptable_designs"):  # heavy rules are not run here: out of scope
        for it in gt.get(name, []):
            if it["rule"].startswith(("hs.", "fit.")):
                it["rule"] = "other:" + it["rule"]
    return gt


def score(findings, gt):
    """-> (classes, status): per finding TP | FP | disputed; per in-scope issue matched | FN | None (low confidence, ignored)."""
    issues = gt.get("issues", [])
    accept = [a for a in gt.get("acceptable_designs", []) if not a["rule"].startswith("other:")]
    hit, classes = set(), []
    for f in findings:
        m = [n for n, it in enumerate(issues) if not it["rule"].startswith("other:") and same(f, it)]
        hit.update(m)
        classes.append("TP" if m else "FP" if any(same(f, a) for a in accept) else "disputed")
    # low-confidence issues are ignored for recall
    status = [(None if it["confidence"] == "low" else "matched" if n in hit else "FN")
              for n, it in enumerate(issues) if not it["rule"].startswith("other:")]
    return classes, status


def evaluate(current=None):
    """-> dict(precision, recall, tp, fp, matched, fn); `current` maps slug -> findings (default: run them)."""
    tp = fp = matched = fn = 0
    for s in slugs():
        classes, status = score(current[s] if current else run_board(s), answer_key(s))
        tp, fp = tp + classes.count("TP"), fp + classes.count("FP")
        matched, fn = matched + status.count("matched"), fn + status.count("FN")
    return dict(precision=tp / (tp + fp) if tp + fp else 1.0, recall=matched / (matched + fn) if matched + fn else 1.0,
                tp=tp, fp=fp, matched=matched, fn=fn)
