#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

# Add project root to Python path when running from scripts/ directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.production_readiness import production_readiness_service


def _print_human(result: dict[str, object]) -> None:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    print(
        "Production readiness: "
        f"{result.get('status')} "
        f"({summary.get('passed', 0)} passed, {summary.get('warning', 0)} warnings, {summary.get('failed', 0)} failed)"
    )
    for item in result.get("items") or []:
        if not isinstance(item, dict):
            continue
        marker = {"passed": "OK", "warning": "WARN", "failed": "FAIL"}.get(str(item.get("status")), "INFO")
        print(f"- [{marker}] {item.get('id')}: {item.get('message')}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check whether the current chatgpt2api configuration is ready for commercial production launch.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print machine-readable JSON output.")
    parser.add_argument(
        "--no-strict",
        action="store_true",
        help="Downgrade APP_ENV!=production to a warning. All infrastructure checks remain enforced.",
    )
    args = parser.parse_args()

    if args.as_json:
        # Some storage backends print initialization diagnostics to stdout.
        # Keep --json stdout machine-readable by moving those diagnostics to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            result = production_readiness_service.check(strict=not args.no_strict)
    else:
        result = production_readiness_service.check(strict=not args.no_strict)
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_human(result)
    return 0 if result.get("ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
