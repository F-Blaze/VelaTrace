"""PR-time regression gate on real boards (see tests/regression/README.md).

Layer 2: findings the answer key marks correct must keep being produced, and new findings must be reviewed.
Layer 3: precision and recall over the whole set must not drop below tests/regression/floors.json.
"""
import json

import pytest

import regression_harness as h

EXPECTED = json.loads(h.EXPECTED.read_text(encoding="utf-8"))
UPDATE = "run `python tools/update_regression.py` and commit expected.json (explain the change in the PR)"
_cache = {}


def current(slug):
    if slug not in _cache:
        _cache[slug] = h.run_board(slug)
    return _cache[slug]


def test_every_board_has_expected_findings():
    assert sorted(EXPECTED) == h.slugs()


@pytest.mark.parametrize("slug", h.slugs())
def test_findings_match_expected(slug):
    now, expected = {h.key(f) for f in current(slug)}, EXPECTED[slug]
    classes, _ = h.score(expected, h.answer_key(slug))
    lost = [h.key(f) for f, c in zip(expected, classes) if c == "TP" and h.key(f) not in now]
    assert not lost, f"{slug}: findings the answer key marks correct are no longer produced: {lost}"
    new = sorted(now - {h.key(f) for f in expected})
    assert not new, (f"{slug}: new findings not in expected.json: {new}. Review them (false positive or real?), "
                     f"then {UPDATE}.")


def test_precision_recall_floors(capsys):
    m = h.evaluate({s: current(s) for s in h.slugs()})
    floors = json.loads(h.FLOORS.read_text(encoding="utf-8"))
    with capsys.disabled():
        print(f"\nregression boards: precision {m['precision']:.4f} ({m['tp']}/{m['tp'] + m['fp']}, floor {floors['precision']})"
              f"  recall {m['recall']:.4f} ({m['matched']}/{m['matched'] + m['fn']}, floor {floors['recall']})")
    assert m["precision"] >= floors["precision"], f"precision {m['precision']:.4f} fell below floor {floors['precision']}"
    assert m["recall"] >= floors["recall"], f"recall {m['recall']:.4f} fell below floor {floors['recall']}"
