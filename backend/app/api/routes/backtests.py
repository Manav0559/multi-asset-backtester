"""Strategy + backtest routes.

Flow: create a Strategy (-> auto v1 StrategyVersion) -> submit a Backtest
(queued row + dispatched Celery task) -> poll the backtest + its yearly
breakdown. Built-in strategies need no code; the `strategy` key in the
backtest config selects the engine strategy.
"""
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.core.config import settings
from app.models import (
    Backtest,
    BacktestYearlyResult,
    OhlcvBar,
    Strategy,
    StrategyVersion,
    User,
)
from app.models.enums import BacktestStatus, Timeframe
from app.schemas.backtest import (
    BacktestCreate,
    BacktestOut,
    StrategyCreate,
    YearlyResultOut,
)

router = APIRouter(tags=["backtests"])


@router.get("/strategies/registry")
def strategy_registry(user: User = Depends(get_current_user)) -> dict:
    """Every runnable strategy (built-in + classic + BYOC), with kind,
    category, and default params — the frontend renders its strategy picker
    and param forms from this, never from a hardcoded list."""
    from app.backtest.registry import STRATEGY_REGISTRY
    from app.backtest.sandbox import DEFAULT_TEMPLATE

    catalog = STRATEGY_REGISTRY.catalog()
    catalog.append({"key": "custom_code", "kind": "single", "category": "custom",
                    "description": "Your own Python strategy (BYOC sandbox)",
                    "defaults": {}})
    return {"strategies": catalog, "custom_template": DEFAULT_TEMPLATE}


@router.post("/strategies/validate")
def validate_custom_code(body: dict, user: User = Depends(get_current_user)) -> dict:
    """Static-check BYOC code without running it — powers the editor's
    'Validate' button. Instantiation errors count too (bad params, no class)."""
    from app.backtest.sandbox import SandboxError, build_custom_strategy

    try:
        strategy = build_custom_strategy(body.get("code", ""), body.get("params"))
    except SandboxError as exc:
        return {"ok": False, "errors": str(exc).split("; ")}
    return {"ok": True, "errors": [], "class_name": type(strategy).__name__}


@router.get("/strategies")
def list_my_strategies(user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)) -> list[dict]:
    """The user's saved strategies (latest version each, with code) — powers
    the BYOC editor's "load a previous script" picker. Only named, code-bearing
    entries are useful there, but everything is returned for completeness."""
    rows = db.execute(
        select(Strategy.id, Strategy.name, StrategyVersion.id.label("version_id"),
               StrategyVersion.version, StrategyVersion.code, Strategy.created_at)
        .join(StrategyVersion, StrategyVersion.strategy_id == Strategy.id)
        .where(Strategy.user_id == user.id)
        .order_by(Strategy.created_at.desc(), StrategyVersion.version.desc())
        .limit(200)
    ).all()
    seen: set = set()
    out = []
    for r in rows:  # keep only the latest version per strategy
        if r.id in seen:
            continue
        seen.add(r.id)
        out.append({"strategy_id": str(r.id), "version_id": str(r.version_id),
                    "name": r.name, "version": r.version, "code": r.code or "",
                    "created_at": r.created_at.isoformat()})
    return out


@router.post("/strategies", status_code=status.HTTP_201_CREATED)
def create_strategy(body: StrategyCreate, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)) -> dict:
    """Create a strategy — or, if the user already has one with this name,
    append a new VERSION. Users iterate on named scripts; a name collision is
    the normal save flow, not an error (uq_strategies_user_name backs this)."""
    strat = db.scalar(select(Strategy).where(
        Strategy.user_id == user.id, Strategy.name == body.name))
    if strat is None:
        strat = Strategy(user_id=user.id, name=body.name)
        db.add(strat)
        db.flush()
        next_version = 1
    else:
        next_version = (db.scalar(
            select(func.max(StrategyVersion.version))
            .where(StrategyVersion.strategy_id == strat.id)) or 0) + 1
    version = StrategyVersion(strategy_id=strat.id, version=next_version,
                              code=body.code, params=body.params)
    db.add(version)
    db.commit()
    return {"strategy_id": str(strat.id), "version_id": str(version.id),
            "version": next_version}


# Working-set model for admission control: bars land in a float frame (~6
# OHLCV cols x 8 bytes) and the engine holds signals/positions/returns/equity
# derivatives on top — 4x covers the measured envelope with slack (ML families
# peaked <300MB on ~1.3K bars incl. model state; the frame dominates at scale).
_BYTES_PER_BAR = 6 * 8
_WORKING_SET_MULTIPLIER = 4


def _estimate_working_set_mb(db: Session, body: BacktestCreate) -> int:
    """Estimated peak memory (MB) for this job, from a chunk-excluded COUNT."""
    asset_ids = body.asset_ids or [body.asset_id]
    try:
        tf = Timeframe(body.timeframe)
    except ValueError:
        return 0  # unknown timeframe fails later with its own 422
    q = (select(func.count()).select_from(OhlcvBar)
         .where(OhlcvBar.asset_id.in_(asset_ids), OhlcvBar.timeframe == tf))
    if body.start:
        q = q.where(OhlcvBar.time >= body.start)
    if body.end:
        q = q.where(OhlcvBar.time <= body.end)
    n_bars = db.scalar(q) or 0
    return (n_bars * _BYTES_PER_BAR * _WORKING_SET_MULTIPLIER) // 2**20


@router.post("/backtests", response_model=BacktestOut, status_code=status.HTTP_202_ACCEPTED)
def submit_backtest(body: BacktestCreate, background: BackgroundTasks,
                    user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)) -> Backtest:
    # Ownership check: the strategy version must belong to this user.
    sv = db.get(StrategyVersion, body.strategy_version_id)
    if sv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "strategy version not found")
    owner_id = db.scalar(select(Strategy.user_id).where(Strategy.id == sv.strategy_id))
    if owner_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your strategy")

    if body.asset_id is None and not body.asset_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "provide asset_id (single) or asset_ids (multi-asset)")
    if body.strategy == "custom_code":
        # Fail fast at submission: reject code the sandbox would refuse anyway,
        # so the user gets a 422 with line numbers instead of a failed run.
        from app.backtest.sandbox import SandboxError, build_custom_strategy
        try:
            build_custom_strategy(body.code or "", body.params)
        except SandboxError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"custom strategy rejected: {exc}") from exc
    # Admission control: estimate the job's working set from a chunk-excluded
    # COUNT (ms post-0008) and reject over-budget jobs NOW with an actionable
    # message — not after minutes of queue time ending in an opaque OOM kill.
    # RLIMIT_AS in the worker remains the backstop; this is the policy.
    est_mb = _estimate_working_set_mb(db, body)
    if est_mb > settings.BACKTEST_MAX_WORKING_SET_MB:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"estimated working set {est_mb}MB exceeds the "
            f"{settings.BACKTEST_MAX_WORKING_SET_MB}MB job budget — narrow the "
            f"date range, use a coarser timeframe (15m/1h/1d), or shrink the "
            f"basket")

    # Human label for result tables: the user's own strategy name makes
    # "custom_code" rows recognizable ("my golden cross v2", not the key).
    label = (body.name.strip() if body.name and body.name.strip()
             else db.scalar(select(Strategy.name).where(Strategy.id == sv.strategy_id)))
    config = {
        "asset_id": body.asset_id, "asset_ids": body.asset_ids,
        "timeframe": body.timeframe,
        "strategy": body.strategy, "params": body.params or {},
        "code": body.code,
        "label": label,
        "start": body.start, "end": body.end,
        "initial_capital": body.initial_capital,
        "commission_bps": body.commission_bps, "slippage_bps": body.slippage_bps,
        "n_trials": body.n_trials,
        "borrow_bps_annual": body.borrow_bps_annual,
        "max_gross_leverage": body.max_gross_leverage,
    }
    bt = Backtest(user_id=user.id, strategy_version_id=sv.id,
                  status=BacktestStatus.QUEUED, config=config)
    db.add(bt)
    db.commit()
    db.refresh(bt)

    # Run in-process after the response is sent (single-process free tier).
    from app.backtest.tasks import execute_backtest
    background.add_task(execute_backtest, bt.id)
    return bt


@router.delete("/backtests/{backtest_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_backtest(backtest_id: uuid.UUID, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)) -> None:
    """Remove a backtest result you own (row + yearly breakdown). Non-owners
    404 (not 403) so result ids can't be probed."""
    bt = db.get(Backtest, backtest_id)
    if bt is None or bt.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "backtest not found")
    db.execute(sa_delete(BacktestYearlyResult)
               .where(BacktestYearlyResult.backtest_id == backtest_id))
    db.delete(bt)
    db.commit()


@router.get("/backtests", response_model=list[BacktestOut])
def list_backtests(user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)) -> list[Backtest]:
    return db.scalars(
        select(Backtest).where(Backtest.user_id == user.id)
        .order_by(Backtest.created_at.desc()).limit(100)
    ).all()


@router.get("/backtests/{backtest_id}", response_model=BacktestOut)
def get_backtest(backtest_id: uuid.UUID, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)) -> Backtest:
    bt = db.get(Backtest, backtest_id)
    if bt is None or bt.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "backtest not found")
    return bt


@router.get("/backtests/{backtest_id}/yearly", response_model=list[YearlyResultOut])
def get_yearly(backtest_id: uuid.UUID, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)) -> list[BacktestYearlyResult]:
    bt = db.get(Backtest, backtest_id)
    if bt is None or bt.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "backtest not found")
    return db.scalars(
        select(BacktestYearlyResult).where(BacktestYearlyResult.backtest_id == backtest_id)
        .order_by(BacktestYearlyResult.year.asc())
    ).all()
