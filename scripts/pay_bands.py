"""Read the posted pay range for each role from GI's own job posts into config/pay-bands.json. Free.

  uv run python scripts/pay_bands.py            # fetch GI's public job board (the cloud blocks it: run on the Mac)
  uv run python scripts/pay_bands.py --file F   # a saved response from the same API

It keeps only a salary range a post states, matched to config/roles.json by the post's id. A role
whose post states none is left out, and the cards say "No posted range on file". It never estimates
anyone's pay. When no post states a range, nothing is written.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pay  # noqa: E402
from app.sources.http import Fetcher  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "config" / "roles.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, help="a saved posting-API response instead of fetching")
    args = parser.parse_args()
    payload = json.loads(args.file.read_text() if args.file else Fetcher(refresh=True)(pay.BOARD))
    found = pay.from_ashby(payload, json.loads(ROLES.read_text())["roles"], date.today().isoformat())
    if not found:
        sys.exit("No post on GI's job board states a salary range; nothing written.")
    pay.BANDS.write_text(json.dumps({"note": f"Read by scripts/pay_bands.py from {pay.BOARD}", "roles": found},
                                    indent=2) + "\n")
    for role_id in found:
        print(f"{role_id}: {pay.line(role_id, found)}")
    print(f"Written to {pay.BANDS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
