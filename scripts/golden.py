"""Score the timing engine on the golden set (tests/golden/*.json). Offline and deterministic.

    uv run python scripts/golden.py [--json] [--min 0.8]

Prints one line per scenario (judgment calls marked and kept out of the rate),
then the pass rate. --min exits non-zero when the rate falls below it.
"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import golden  # noqa: E402


def line(r):
    expected = r["expected"]["call"] + (f" {'..'.join(r['expected']['until_between'])}" if "until_between" in r["expected"] else "")
    got = r["call"] + (f" {r['until']}" if r["until"] else "")
    mark = "judgment " if r["judgment"] else ""
    out = f"{r['outcome']:12} {mark}{r['id']}: expected {expected}, got {got}"
    if r["outcome"] != "pass":
        out += f"\n             {r['explanation']}"
        if r["uncited"]:
            out += f"\n             unread evidence: {', '.join(r['uncited'])}"
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="print every row and the summary as JSON")
    parser.add_argument("--min", type=float, help="fail when the pass rate is below this")
    args = parser.parse_args()
    rows = golden.evaluate()
    stats = golden.summary(rows)
    if args.json:
        print(json.dumps({"summary": stats, "rows": rows}, indent=2))
    else:
        print("\n".join(line(r) for r in rows))
        print(f"\npass rate {stats['passed']}/{stats['scored']} = {stats['pass_rate']:.0%}"
              f" ({', '.join(f'{role} {rate}' for role, rate in stats['roles'].items())});"
              f" judgment calls agreed {stats['judgment_agreed']}/{stats['judgment']}")
    if args.min is not None and stats["pass_rate"] < args.min:
        sys.exit(1)


if __name__ == "__main__":
    main()
