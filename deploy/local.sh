#!/usr/bin/env sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
python3 deploy/init.py
docker compose up --build -d --wait
python3 deploy/smoke.py --docker
