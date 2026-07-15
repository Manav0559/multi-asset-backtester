#!/usr/bin/env sh
# Container entrypoint (Render + local Docker): migrate to head, then serve.
#
# Deliberately a script, not an inline `dockerCommand`, so nothing has to parse a
# quoted `sh -c '... && ...'` string — the previous inline form exited 127 when
# the host split it on whitespace and mangled the quotes. `python -m` avoids
# depending on console-script shims being on PATH, and `exec` hands PID 1 to
# uvicorn so signals (SIGTERM on deploy/scale-down) reach it directly.
set -e

python -m alembic upgrade head
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
