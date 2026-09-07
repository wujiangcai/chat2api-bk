"""Strip archived b64 payloads from image_jobs.json so poll/save stay fast."""

from __future__ import annotations

import json
from pathlib import Path
import sys


def compact(path: Path) -> int:
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise SystemExit(f"unexpected image_jobs shape in {path}")
    changed_jobs = 0
    for job in items:
        if not isinstance(job, dict):
            continue
        result = job.get("result")
        if not isinstance(result, dict):
            continue
        data = result.get("data")
        if not isinstance(data, list):
            continue
        assets = job.get("assets") or []
        has_asset_url = any(isinstance(item, dict) and item.get("url") for item in assets)
        slim_data = []
        changed = False
        for item in data:
            if (
                isinstance(item, dict)
                and item.get("b64_json")
                and (item.get("url") or has_asset_url)
            ):
                slim_data.append({key: value for key, value in item.items() if key != "b64_json"})
                changed = True
            else:
                slim_data.append(item)
        if changed:
            result["data"] = slim_data
            changed_jobs += 1
    if changed_jobs:
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed_jobs


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "data/image_jobs.json")
    before = target.stat().st_size if target.exists() else 0
    n = compact(target)
    after = target.stat().st_size
    print(f"compacted {n} jobs; {before} -> {after} bytes")
