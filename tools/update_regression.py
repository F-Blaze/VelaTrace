"""Re-snapshot the findings of the regression boards into tests/regression/expected.json.

Run after a deliberate change to the audit checks, review the diff, and explain it in the PR.
`--floors` also resets tests/regression/floors.json to today's precision/recall (only to lock in an
improvement; lowering a floor needs a reason in the PR).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

import regression_harness as h  # noqa: E402


def main():
    current = {s: sorted(h.run_board(s), key=h.key) for s in h.slugs()}
    h.EXPECTED.write_text(json.dumps(current, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {h.EXPECTED.relative_to(ROOT)}: {sum(map(len, current.values()))} findings on {len(current)} boards")
    m = h.evaluate(current)
    print(f"precision {m['precision']:.4f} ({m['tp']}/{m['tp'] + m['fp']})  recall {m['recall']:.4f} ({m['matched']}/{m['matched'] + m['fn']})")
    if "--floors" in sys.argv:  # rounded down so today's values pass
        floors = {k: int(m[k] * 10000) / 10000 for k in ("precision", "recall")}
        h.FLOORS.write_text(json.dumps(floors, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {h.FLOORS.relative_to(ROOT)}: {floors}")


if __name__ == "__main__":
    main()
