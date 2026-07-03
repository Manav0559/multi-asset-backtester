# Contributing

## Dev loop

```bash
docker compose up -d                 # infra only: TimescaleDB :5433 + Redis :6380

cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
alembic upgrade head
uvicorn app.main:app --port 8000
# second shell — worker + beat:
celery -A app.backtest.tasks:celery_app worker -B --loglevel=info

cd ../frontend && npm install && npm run dev
```

Full stack: `docker compose --profile app up -d --build`.
Populated demo: `./scripts/demo.sh`.

## Tests

```bash
cd backend && python -m pytest tests/ -q     # needs docker db + redis up
cd frontend && npm run lint && npm run build
```

Tests run against the live docker DB with uniquely-suffixed users and clean up
after themselves — the suite is safe to re-run against a dev database. CI does
exactly this against ephemeral service containers.

## Conventions (non-negotiable)

- **No lookahead in backtests.** `position = signal.shift(1)` is the engine's
  guarantee; strategies must be strictly backward-looking too (no `.shift(-1)`,
  no future indexing). New indicator/strategy code needs a
  truncation-invariance test (see `test_classic_single_no_lookahead`).
- **The signal contract is fixed:** strategies emit target weights per asset
  per bar (`>0` long, `<0` short, `sum(|w|)` = gross). Register new strategies
  in `app/backtest/registry.py`; the frontend picks them up automatically.
- **Shared cash is mutated only inside a `SELECT … FOR UPDATE` transaction**
  (`services/execution.py`); publish `portfolio:{id}` events after commit.
- **Money is `Decimal`** end-to-end into `Numeric` columns. Floats are for
  return series inside the engine, never for balances.
- **Report Deflated Sharpe** alongside raw Sharpe for anything that produces
  a performance number.
- Schema changes go through Alembic (`backend/alembic/versions/`), and
  anything user-facing that computes an indicator must go through
  `IndicatorService` so charts and backtests can't disagree.
- Comments explain **why**, not what. Match the style around you.

## Pull requests

Keep PRs scoped; include the failing-then-passing test for behavior changes.
CI (backend suite + frontend lint/build) must be green.
