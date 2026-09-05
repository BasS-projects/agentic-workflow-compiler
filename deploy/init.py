"""Generate an isolated local deployment's credentials without reusable secrets.

The private parent directory protects bind-mounted, container-readable JSON.
Run `python deploy/init.py --token operator` to reveal one token intentionally.
Existing complete configuration is preserved; partial configuration fails closed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys


ROOT = Path(__file__).resolve().parents[1]
ROLES = ("viewer", "operator", "approver", "admin", "worker")


def write_private(path: Path, content: str, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        target.write(content)
    path.chmod(mode)


def initialize(directory: Path) -> dict:
    if directory.is_symlink():
        raise ValueError("Deployment directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    managed = [directory / name for name in ("auth.json", "tokens.json", "worker.env", "plugins.json")]
    if any(path.is_symlink() for path in managed):
        raise ValueError("Deployment configuration files must not be symlinks")
    present = [path.exists() for path in managed]
    if any(present):
        if not all(present):
            raise ValueError("Partial deployment configuration found. Restore its missing files or choose a new --directory; credentials were not overwritten.")
        tokens = json.loads((directory / "tokens.json").read_text(encoding="utf-8"))
        auth = json.loads((directory / "auth.json").read_text(encoding="utf-8"))
        if set(tokens) != set(ROLES) or any(auth.get(tokens[role], {}).get("role") != role for role in ROLES):
            raise ValueError("Existing token and authorization files disagree; credentials were not overwritten")
        return tokens
    tokens = {role: secrets.token_urlsafe(36) for role in ROLES}
    auth = {
        token: {"actor": "worker-1" if role == "worker" else "local-" + role, "role": role}
        for role, token in tokens.items()
    }
    # File binds need read permission for container UID 10001; local protection
    # comes from the owner-only (0700) parent, not a hardcoded host/container UID.
    write_private(directory / "auth.json", json.dumps(auth, indent=2) + "\n", 0o644)
    write_private(directory / "tokens.json", json.dumps(tokens, indent=2) + "\n", 0o600)
    write_private(directory / "worker.env", "AWC_WORKER_TOKEN=" + tokens["worker"] + "\n", 0o600)
    write_private(directory / "plugins.json", '{"plugins": []}\n', 0o644)
    return tokens


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / ".state" / "deploy")
    parser.add_argument("--token", choices=ROLES, help="Print only the requested token; treat terminal output as secret")
    args = parser.parse_args()
    try:
        tokens = initialize(args.directory)
    except (OSError, ValueError, TypeError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        return 1
    if args.token:
        print(tokens[args.token])
    else:
        print(f"Deployment configuration ready: {args.directory}")
        print("Start: docker compose up --build -d")
        print("Console: http://127.0.0.1:8080")
        print("Show an operator token: python deploy/init.py --token operator")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
