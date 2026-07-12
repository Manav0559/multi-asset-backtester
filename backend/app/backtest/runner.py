"""Backtest runner — orchestrates load → run → measure → persist.

Loads historical bars for one asset+timeframe from ohlcv_bars into a
DataFrame, runs the vectorized engine with the requested built-in strategy,
computes headline + yearly metrics + Deflated Sharpe, and writes everything
back to the backtests / backtest_yearly_results rows.

`config` shape (stored on the Backtest row for reproducibility):
  {
    "asset_id": int, "timeframe": "1d",
    "strategy": "sma_crossover", "params": {"fast": 20, "slow": 50},
    "start": "2020-01-01", "end": "2025-01-01",
    "initial_capital": 100000, "commission_bps": 0, "n_trials": 1
  }
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtest.engine import run_backtest
from app.backtest.metrics import (
    compute_metrics,
    deflated_sharpe_ratio,
    tail_risk_metrics,
    yearly_breakdown,
)
from app.backtest.registry import STRATEGY_REGISTRY
from app.core.config import settings
from app.db.session import SessionLocal
from app.models import Asset, Backtest, BacktestYearlyResult, OhlcvBar
from app.models.enums import BacktestStatus, Timeframe

logger = logging.getLogger("backtest.runner")


class BacktestConfigError(Exception):
    pass


def load_bars(db: Session, asset_id: int, timeframe: Timeframe,
              start: datetime | None, end: datetime | None) -> pd.DataFrame:
    q = (select(OhlcvBar.time, OhlcvBar.open, OhlcvBar.high, OhlcvBar.low,
                OhlcvBar.close, OhlcvBar.volume)
         .where(OhlcvBar.asset_id == asset_id, OhlcvBar.timeframe == timeframe)
         .order_by(OhlcvBar.time.asc()))
    if start:
        q = q.where(OhlcvBar.time >= start)
    if end:
        q = q.where(OhlcvBar.time <= end)
    rows = db.execute(q).all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df = df.set_index(pd.DatetimeIndex(df["time"])).drop(columns=["time"])
    return df.astype(float)


def load_panel(db: Session, asset_ids: list[int], timeframe: Timeframe,
               start: datetime | None, end: datetime | None):
    """Load a wide (close, volume) price panel across assets for the
    multi-asset engine. Columns are asset_ids (as strings, JSON-safe).
    Inner-joins on the shared trading calendar so all assets align."""
    closes, volumes = {}, {}
    for aid in asset_ids:
        bars = load_bars(db, aid, timeframe, start, end)
        if bars.empty:
            continue
        closes[str(aid)] = bars["close"]
        volumes[str(aid)] = bars["volume"]
    if not closes:
        return pd.DataFrame(), pd.DataFrame()
    close_panel = pd.DataFrame(closes).dropna()
    vol_panel = pd.DataFrame(volumes).reindex(close_panel.index)
    return close_panel, vol_panel


def _record_and_count_trial(cfg: dict, params: dict) -> int:
    """Log this ML backtest as a trial and return how many trials share its
    research question (family + asset). That count is the REAL N for the
    Deflated Sharpe multiple-testing correction — not a client-supplied guess."""
    import hashlib
    import json as _json

    from sqlalchemy import func, select

    from app.models import MlTrial

    model_id = params.get("model_id", "xgboost")
    research_key = f"{model_id}:{cfg.get('asset_id')}"
    phash = hashlib.sha1(
        _json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]
    with SessionLocal() as db:
        db.add(MlTrial(research_key=research_key, params_hash=phash))
        db.commit()
        return db.scalar(select(func.count()).select_from(MlTrial)
                         .where(MlTrial.research_key == research_key)) or 1


def _build_strategy(name: str, params: dict):
    if name not in STRATEGY_REGISTRY:
        raise BacktestConfigError(f"unknown strategy '{name}'")
    try:
        return STRATEGY_REGISTRY.build(name, params)
    except (TypeError, ValueError) as exc:
        raise BacktestConfigError(f"bad params for {name}: {exc}") from exc


def _parse_dt(v):
    if not v:
        return None
    return datetime.fromisoformat(v).replace(tzinfo=timezone.utc) \
        if "T" not in str(v) and "+" not in str(v) else datetime.fromisoformat(v)


def run_and_persist(backtest_id: uuid.UUID) -> None:
    """Execute the backtest identified by an existing QUEUED row."""
    with SessionLocal() as db:
        bt = db.get(Backtest, backtest_id)
        if bt is None:
            raise BacktestConfigError(f"backtest {backtest_id} not found")
        bt.status = BacktestStatus.RUNNING
        bt.started_at = datetime.now(timezone.utc)
        db.commit()
        cfg = dict(bt.config)

    try:
        result = _execute(cfg)
    except Exception as exc:  # noqa: BLE001 — record failure, don't crash the worker
        with SessionLocal() as db:
            bt = db.get(Backtest, backtest_id)
            bt.status = BacktestStatus.FAILED
            bt.error = f"{type(exc).__name__}: {exc}"
            bt.finished_at = datetime.now(timezone.utc)
            db.commit()
        raise

    _persist(backtest_id, result)


def _execute(cfg: dict) -> dict:
    # Dispatch on the registry's kind: "portfolio" strategies use the
    # weight-based engine; "single" (incl. BYOC custom code) the single-asset
    # engine.
    name = cfg["strategy"]
    if name != "custom_code" and name not in STRATEGY_REGISTRY:
        raise BacktestConfigError(f"unknown strategy '{name}'")
    if name != "custom_code" and STRATEGY_REGISTRY.kind(name) == "portfolio":
        out, diagnostics = _execute_portfolio(cfg)
    else:
        out, diagnostics = _execute_single_asset(cfg)

    metrics = compute_metrics(out.returns, out.equity, out.trades,
                              rf=settings.BACKTEST_RISK_FREE_RATE)
    yearly = yearly_breakdown(out.returns, out.equity, out.trades,
                              rf=settings.BACKTEST_RISK_FREE_RATE)
    dsr = deflated_sharpe_ratio(
        out.returns, int(cfg.get("n_trials", settings.BACKTEST_DEFAULT_TRIALS)))

    # Tail risk (VaR/ES/Cornish-Fisher) rides the diagnostics JSON — same
    # per-period frequency as the bar series, formatted by the report layer.
    risk = tail_risk_metrics(out.returns)
    if risk:
        diagnostics = {**(diagnostics or {}), "risk": risk}

    # Factor attribution vs the stored universe (same asset class). A report
    # nicety: any failure or thin universe just omits the card.
    attribution = _compute_attribution(cfg, out.returns)
    if attribution:
        diagnostics = {**(diagnostics or {}), "attribution": attribution}

    # Downsample equity curve to <=1000 points for charting.
    step = max(len(out.equity) // 1000, 1)
    curve = [[ts.isoformat(), round(float(v), 2)]
             for ts, v in out.equity.iloc[::step].items()]

    return {"metrics": metrics, "yearly": yearly, "dsr": dsr, "curve": curve,
            "diagnostics": diagnostics}


_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


def _compute_attribution(cfg: dict, returns: pd.Series) -> dict | None:
    """Regress the run's returns on MKT/MOM/LIQ factors built from every
    stored asset of the SAME class. Union-aligned panels (an asset missing
    bars contributes NaN, not a dropped row — load_panel's inner join would
    let one late-listed name truncate the whole factor history)."""
    from app.backtest.attribution import MIN_OBS, MIN_UNIVERSE, attribute, build_factors

    try:
        timeframe = Timeframe(cfg.get("timeframe", "1d"))
        anchor_ids = cfg.get("asset_ids") or ([cfg.get("asset_id")] if cfg.get("asset_id") else [])
        if not anchor_ids or len(returns) < MIN_OBS:
            return None
        # Warm-up so the 126-bar momentum score exists from the first return
        # bar (x2 for calendar gaps — weekends/closed sessions).
        warmup = pd.Timedelta(seconds=_TF_SECONDS.get(timeframe.value, 86400) * 300 * 2)
        start = returns.index[0].to_pydatetime() - warmup
        end = returns.index[-1].to_pydatetime()

        with SessionLocal() as db:
            klass = db.scalar(select(Asset.asset_class).where(Asset.id == int(anchor_ids[0])))
            ids = db.scalars(select(Asset.id).where(Asset.asset_class == klass)).all()
            if len(ids) < MIN_UNIVERSE:
                return None
            closes, volumes = {}, {}
            for aid in ids:
                bars = load_bars(db, aid, timeframe, start, end)
                if len(bars) >= MIN_OBS:
                    closes[str(aid)] = bars["close"]
                    volumes[str(aid)] = bars["volume"]
        if len(closes) < MIN_UNIVERSE:
            return None
        factors = build_factors(pd.DataFrame(closes), pd.DataFrame(volumes))
        return attribute(returns, factors)
    except Exception:  # noqa: BLE001 — never fail a backtest over its report card
        logger.warning("factor attribution skipped", exc_info=True)
        return None


def _execute_single_asset(cfg: dict):
    """Single-asset long/flat engine (+ ML). Returns (output, diagnostics)."""
    timeframe = Timeframe(cfg.get("timeframe", "1d"))
    with SessionLocal() as db:
        df = load_bars(db, int(cfg["asset_id"]), timeframe,
                       _parse_dt(cfg.get("start")), _parse_dt(cfg.get("end")))
    if df.empty:
        raise BacktestConfigError("no historical bars for the requested window")

    diagnostics: dict | None = None
    params = cfg.get("params", {})
    if cfg["strategy"] == "custom_code":
        from app.backtest.sandbox import (
            SandboxError,
            build_custom_strategy,
            run_custom_strategy,
        )
        try:
            custom = build_custom_strategy(cfg.get("code", ""), params)
        except SandboxError as exc:
            raise BacktestConfigError(f"custom strategy rejected: {exc}") from exc
        strategy = lambda _df: run_custom_strategy(custom, _df)  # noqa: E731
        diagnostics = {"custom_class": type(custom).__name__,
                       "code_bytes": len(cfg.get("code", "").encode())}
    elif cfg["strategy"] == "ml_direction" or cfg["strategy"].startswith("ml_"):
        from app.ml.model import MODEL_FAMILIES, run_ml_direction, run_ml_meta
        # `ml_direction` is the xgboost default; `ml_<family>` picks a family;
        # `ml_meta_momentum` is the meta-labeling pipeline (same CV hygiene,
        # purge = barrier max_horizon instead of the fixed label horizon).
        p = dict(params or {})
        if cfg["strategy"] == "ml_meta_momentum":
            p["model_id"] = "meta_momentum"       # its own DSR research key
            n_trials = _record_and_count_trial(cfg, p)
            p.pop("model_id")
            ml = run_ml_meta(df, **p)
        else:
            if cfg["strategy"] != "ml_direction":
                fam = cfg["strategy"][len("ml_"):]
                if fam not in MODEL_FAMILIES:
                    raise BacktestConfigError(f"unknown ML family '{fam}'")
                p["model_id"] = fam
            # Record trials for the Deflated Sharpe multiple-testing correction.
            n_trials = _record_and_count_trial(cfg, p)
            ml = run_ml_direction(df, **p)
        signal = ml.signal
        strategy = lambda _df: signal.reindex(_df.index).fillna(0.0)  # noqa: E731
        diagnostics = {
            "model_id": ml.model_id,
            "oos_accuracy": round(ml.oos_accuracy, 4),
            "n_predictions": ml.n_predictions,
            "brier_score": round(ml.brier_score, 4) if ml.brier_score is not None else None,
            "baseline_oos_accuracy": round(ml.baseline_oos_accuracy, 4)
                if ml.baseline_oos_accuracy is not None else None,
            "fold_metrics": ml.fold_metrics,
            "n_trials": n_trials,
            "feature_importance": ml.feature_importance,
            "feature_importance_std": ml.feature_importance_std,
        }
        cfg = {**cfg, "n_trials": n_trials}  # feed the real N into DSR downstream
    else:
        strategy = _build_strategy(cfg["strategy"], params)

    out = run_backtest(
        df, strategy,
        initial_capital=float(cfg.get("initial_capital", 100_000)),
        commission_bps=float(cfg.get("commission_bps", settings.COMMISSION_BPS)),
    )
    return out, diagnostics


def _execute_portfolio(cfg: dict):
    """Long/short, multi-asset weight engine. Returns (output, diagnostics)."""
    from app.backtest.costs import CostModel
    from app.backtest.portfolio_engine import run_portfolio_backtest
    from app.backtest.portfolio_strategies import PORTFOLIO_STRATEGIES

    timeframe = Timeframe(cfg.get("timeframe", "1d"))
    asset_ids = cfg.get("asset_ids") or ([cfg["asset_id"]] if cfg.get("asset_id") else [])
    asset_ids = [int(a) for a in asset_ids]
    if not asset_ids:
        raise BacktestConfigError("portfolio backtest requires asset_ids")

    with SessionLocal() as db:
        close_panel, vol_panel = load_panel(
            db, asset_ids, timeframe, _parse_dt(cfg.get("start")), _parse_dt(cfg.get("end")))
    if close_panel.empty:
        raise BacktestConfigError("no aligned historical bars for the requested assets/window")

    params = cfg.get("params", {})
    try:
        strategy = PORTFOLIO_STRATEGIES[cfg["strategy"]](**(params or {}))
    except TypeError as exc:
        raise BacktestConfigError(f"bad params for {cfg['strategy']}: {exc}") from exc

    out = run_portfolio_backtest(
        close_panel, strategy, volumes=vol_panel,
        initial_capital=float(cfg.get("initial_capital", 100_000)),
        cost_model=CostModel(commission_bps=float(cfg.get("commission_bps", settings.COMMISSION_BPS))),
        borrow_bps_annual=float(cfg.get("borrow_bps_annual", 0.0)),
        max_gross_leverage=cfg.get("max_gross_leverage"),
    )
    active = out.gross_exposure[out.gross_exposure > 0]
    diagnostics = {
        "n_assets": len(asset_ids),
        "avg_gross_exposure": round(float(active.mean()), 4) if len(active) else 0.0,
        "max_net_exposure": round(float(out.net_exposure.abs().max()), 4),
        "is_market_neutral": bool(out.net_exposure.abs().max() < 1e-6),
    }
    return out, diagnostics


def _persist(backtest_id: uuid.UUID, result: dict) -> None:
    m = result["metrics"]
    with SessionLocal() as db:
        bt = db.get(Backtest, backtest_id)
        bt.status = BacktestStatus.COMPLETED
        bt.finished_at = datetime.now(timezone.utc)
        bt.total_return_pct = _dec(m.total_return_pct)
        bt.cagr_pct = _dec(m.cagr_pct)
        bt.sharpe = _dec(m.sharpe)
        bt.sortino = _dec(m.sortino)
        bt.deflated_sharpe = _dec(result["dsr"])
        bt.max_drawdown_pct = _dec(m.max_drawdown_pct)
        bt.trade_count = m.trade_count
        bt.win_rate_pct = _dec(m.win_rate_pct)
        bt.equity_curve = result["curve"]
        bt.diagnostics = result.get("diagnostics")
        db.add(bt)
        for y in result["yearly"]:
            db.add(BacktestYearlyResult(
                backtest_id=backtest_id, year=y.year,
                return_pct=_dec(y.return_pct), max_drawdown_pct=_dec(y.max_drawdown_pct),
                sharpe=_dec(y.sharpe), sortino=_dec(y.sortino),
                volatility_pct=_dec(y.volatility_pct), trade_count=y.trade_count,
                win_rate_pct=_dec(y.win_rate_pct),
            ))
        db.commit()


def _dec(v) -> Decimal | None:
    if v is None or (isinstance(v, float) and (v != v)):  # None or NaN
        return None
    return Decimal(str(round(float(v), 4)))
