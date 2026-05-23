#!/usr/bin/env bash
# Launch BirdNET server. Listens on 0.0.0.0:8000.
# Pair with: tailscale funnel --bg 8000   (or :8443 → 8000)
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
exec uvicorn server.app:app --host 0.0.0.0 --port 8000 --log-level info "$@"
