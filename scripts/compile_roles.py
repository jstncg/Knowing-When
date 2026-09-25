"""Compile the watched roles' JDs (MTS, backend engineer, Product Designer) into role specs, side by side.

  uv run python scripts/compile_roles.py [--write]

Paid: one Claude call per configured role (two when it needs a repair). A new JD
is added with scripts/roles.py add. roles.json keeps the verified brief,
not the full JD text, so the configured JDs are rebuilt from it. --write
replaces config/role-specs/<role id>.json for the configured roles, which
changes what readiness counts for them.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import settings  # noqa: E402
from app.providers import ProviderError  # noqa: E402
from app.role_compiler import (  # noqa: E402
    build_signal_plan, compile_role, side_by_side, spec_path,
)

CONFIGURED = ("mts-research", "backend", "product-designer")


def brief_jd(role: dict) -> str:
    preferred = [f"- {c}" for c in role["criteria"] if c not in role["required_criteria"]]
    lines = [role["title"], role["interpretation"], "Required:", *[f"- {c}" for c in role["required_criteria"]]]
    return "\n".join(lines + (["Preferred:", *preferred] if preferred else []) + role["manager_preferences"])


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="replace the configured roles' specs")
    args = parser.parse_args()
    roles = {r["id"]: r for r in json.loads((ROOT / "config/roles.json").read_text())["roles"]}
    config = {**settings(), "max_calls_per_run": 4}
    compiled = []
    try:
        for role_id in CONFIGURED:
            role = roles[role_id]
            spec = await compile_role(brief_jd(role), role_id, config, build_signal_plan(role)["signals"])
            if args.write:
                spec_path(role_id).write_text(spec.model_dump_json(indent=1) + "\n")
            compiled.append(spec.model_dump())
    except ProviderError as exc:
        sys.exit(str(exc))
    print(side_by_side(compiled))


if __name__ == "__main__":
    asyncio.run(main())
