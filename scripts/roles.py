"""Add a role from its job description, or take a dropped-in role out: the web app's "Drop in a JD" from a terminal.

  uv run python scripts/roles.py add --jd path/to/jd.txt [--url LINK] [--office CITY ...] [--yes]
  uv run python scripts/roles.py add --demo
  uv run python scripts/roles.py remove ROLE_ID

add reads the JD into a role and prints what the engine would watch for it; it asks before writing
config/roles.json and config/role-specs/<id>.json (--yes writes without asking). Paid: two model calls
(role_drop.PRICE). --demo reads the invented sample JD from answers written by hand in the model's format, free,
and writes nothing. remove takes out a role that came in this way; the roles configured by hand stay. A running
web app shows a role added or removed here from its next start.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import role_drop  # noqa: E402
from app.main import settings  # noqa: E402
from app.providers import ProviderError  # noqa: E402


def show(p):
    w, r = p["watch"], p["role"]
    lines = [f"{r['title']} ({r['id']}), {r['lane']}", "", "Counts for this role:"]
    lines += [f"- {s['means']}\n    Here: {s['why']}" for s in w["signs"]] or ["- Nothing beyond what counts for every role."]
    lines += ["", w["every_role"], "", w["holds"], "", "Close to its work: " + ("; ".join(w["topics"]) or "none")]
    lines += ["", "What it can't watch:"] + [f"- {g}" for g in w["gaps"]]
    lines += ["", "Before its calls go out:"] + [f"- {b}" for b in p["before"]]
    if p["cost"]:
        lines += ["", f"This read cost ${p['cost']['usd']:.2f}." if p["cost"]["usd"] is not None else
                  "This read's cost is priced only for the default model."]
    if p["clash"]:
        lines += ["", p["clash"]]
    return "\n".join(line if line.startswith("-") or not line else textwrap.fill(line, 100) for line in lines)


async def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="read a JD into a role, then add it")
    add.add_argument("--jd", type=Path, help="the JD as plain text (.txt or .md)")
    add.add_argument("--url", default="", help="its job post's link, optional")
    add.add_argument("--office", action="append", default=[], help="an office it hires for; repeat for more")
    add.add_argument("--yes", action="store_true", help="write without asking")
    add.add_argument("--demo", action="store_true", help="the invented sample JD, answered by hand; writes nothing")
    remove = commands.add_parser("remove", help="take out a dropped-in role")
    remove.add_argument("role_id")
    args = parser.parse_args()

    if args.command == "remove":
        try:
            role = role_drop.remove(args.role_id)
        except (KeyError, ValueError) as e:
            sys.exit(str(e).strip("'\""))
        print(f"Removed {role['title']} ({role['id']}).")
        return
    if args.demo:
        print(show(role_drop.sample()) + "\n\nThe sample JD is invented: it previews, it never becomes a role.")
        return
    if not args.jd:
        parser.error("add needs --jd FILE, or --demo")
    print(role_drop.PRICE)
    try:
        previewed = await role_drop.preview(args.jd.read_text(), settings(), args.url)
    except ProviderError as e:
        sys.exit(str(e))
    print("\n" + show(previewed))
    if previewed["clash"]:
        sys.exit(1)
    if not args.yes and (not sys.stdin.isatty() or input("\nAdd this role? [y/N] ").strip().lower() != "y"):
        sys.exit("Nothing written.")
    role = role_drop.accept(previewed, args.url, args.office)
    print(f"Added {role['title']} ({role['id']}).\n" + "\n".join(f"- {line}" for line in role_drop.ADDED))


if __name__ == "__main__":
    asyncio.run(main())
