#!/usr/bin/env python3
"""
Check that every handler has a top-level event schema.

Looks for files:
  protocols/<protocol>/handlers/<type>/<type>_handler.json
And ensures the JSON contains a "schema" key.

Exit code 0 if all good, 1 if any missing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    missing: list[str] = []
    checked: int = 0

    for proto in (root / "protocols").iterdir():
        if not proto.is_dir():
            continue
        handlers = proto / "handlers"
        if not handlers.exists():
            continue
        for handler_dir in handlers.iterdir():
            if not handler_dir.is_dir():
                continue
            config = handler_dir / f"{handler_dir.name}_handler.json"
            if not config.exists():
                continue
            checked += 1
            try:
                data = json.loads(config.read_text())
            except Exception as e:
                missing.append(f"{config} (invalid JSON: {e})")
                continue
            if "schema" not in data or not isinstance(data.get("schema"), dict):
                missing.append(str(config))

    if missing:
        print("Missing handler schemas (required):")
        for m in missing:
            print(f" - {m}")
        print(f"Checked {checked} handler configs; {len(missing)} missing schemas.")
        return 1

    print(f"All handler configs have schemas. Checked {checked}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

