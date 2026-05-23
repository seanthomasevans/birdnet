#!/usr/bin/env bash
# Launch BirdNET server. Listens on 0.0.0.0:8788.
# (Port 8000 is taken by the conformer/narrative server.)
# Pair with: tailscale funnel --bg --https=8443 8788
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
exec uvicorn server.app:app --host 0.0.0.0 --port 8788 --log-level info "$@"
