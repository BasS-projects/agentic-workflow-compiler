"""Container health probes. Never print credentials or execution payloads."""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path


def main() -> int:
    try:
        if len(sys.argv) >= 2 and sys.argv[1] == "api":
            with urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=3) as response:
                payload = json.load(response)
                if response.status != 200 or not isinstance(payload, dict):
                    return 1
            return 0
        if len(sys.argv) == 3 and sys.argv[1] == "worker":
            path = Path(sys.argv[2])
            state = json.loads(path.read_text(encoding="utf-8"))
            age = time.time() - path.stat().st_mtime
            return 0 if state.get("status") in {"idle", "executing"} and -5 <= age <= 120 else 1
        return 2
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
