#!/usr/bin/env bash
# One-command demo: full stack + real market data + populated portfolios,
# leaderboard, and one backtest of every kind.
#
#   ./scripts/demo.sh
#
# Ports are overridable the same way as compose itself:
#   FRONTEND_PORT=3005 ./scripts/demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> bringing up the full stack (docker compose --profile app)"
docker compose --profile app up -d --build

echo "==> seeding demo data inside the backend container"
docker compose --profile app exec \
  -e FRONTEND_URL="http://localhost:${FRONTEND_PORT:-3000}" \
  backend python scripts/demo_seed.py
