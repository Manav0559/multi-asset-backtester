"""Triple-barrier labels + meta-labeling: barrier logic proven on constructed
paths, side-orientation proven, and the meta pipeline run end-to-end."""
import numpy as np
import pandas as pd
import pytest

from app.ml.labels import triple_barrier_label


def _px(moves, warmup=60, warmup_step=0.001):
    """Price series: a warm-up of small alternating moves (so EWMA sigma is
    small and stable), then the constructed event path."""
    steps = [warmup_step * (1 if i % 2 == 0 else -1) for i in range(warmup)] + list(moves)
    px = 100 * np.cumprod(1 + np.array([0.0] + steps))
    return pd.Series(px, index=pd.date_range("2024-01-01", periods=len(px), freq="D"))


def test_profit_take_touched_first():
    # After warm-up, bar t: next bars rally hard -> PT (2 sigma, sigma~0.1%)
    # is hit on the first forward bar.
    close = _px([0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    lab = triple_barrier_label(close, pt_mult=2.0, sl_mult=2.0, max_horizon=5,
                               vol_lookback=20)
    t = close.index[60]  # the bar right before the +5% jump
    assert lab.loc[t, "label"] == 1
    assert lab.loc[t, "touch_bars"] == 1
    assert lab.loc[t, "touch_ret"] > 0.04


def test_stop_loss_touched_first():
    close = _px([-0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    lab = triple_barrier_label(close, pt_mult=2.0, sl_mult=2.0, max_horizon=5,
                               vol_lookback=20)
    t = close.index[60]
    assert lab.loc[t, "label"] == -1
    assert lab.loc[t, "touch_bars"] == 1
    assert lab.loc[t, "touch_ret"] < -0.04


def test_vertical_barrier_timeout():
    # Flat forward path: neither barrier inside the window -> 0 at max_horizon.
    close = _px([0.0] * 12)
    lab = triple_barrier_label(close, pt_mult=5.0, sl_mult=5.0, max_horizon=5,
                               vol_lookback=20)
    t = close.index[60]
    assert lab.loc[t, "label"] == 0
    assert lab.loc[t, "touch_bars"] == 5


def test_side_orients_the_barriers():
    """For a SHORT (side=-1) a crash is the PROFIT-take: the same down-path
    that labels -1 long must label +1 short."""
    close = _px([-0.05] + [0.0] * 10)
    short_side = pd.Series(-1.0, index=close.index)
    lab_long = triple_barrier_label(close, pt_mult=2.0, sl_mult=2.0,
                                    max_horizon=5, vol_lookback=20)
    lab_short = triple_barrier_label(close, pt_mult=2.0, sl_mult=2.0,
                                     max_horizon=5, vol_lookback=20,
                                     side=short_side)
    t = close.index[60]
    assert lab_long.loc[t, "label"] == -1
    assert lab_short.loc[t, "label"] == 1


def test_labels_are_forward_looking_only_by_the_window():
    """Truncating the series changes NOTHING for bars whose full forward
    window survives — the label depends only on [t, t+max_horizon]."""
    rng = np.random.default_rng(21)
    close = pd.Series(
        100 * np.exp(np.cumsum(rng.normal(0, 0.01, 300))),
        index=pd.date_range("2023-01-01", periods=300, freq="D"))
    full = triple_barrier_label(close, max_horizon=10)
    trunc = triple_barrier_label(close.iloc[:200], max_horizon=10)
    shared = trunc.index
    pd.testing.assert_frame_equal(full.loc[shared], trunc)


def test_meta_pipeline_end_to_end():
    """run_ml_meta on a noisy trending series: long-only proba-sized signal,
    honest diagnostics, primary baseline present."""
    from app.ml.model import run_ml_meta

    rng = np.random.default_rng(4)
    n = 600
    drift = np.where(np.arange(n) % 200 < 120, 0.0015, -0.001)  # regimes
    close = pd.Series(100 * np.exp(np.cumsum(drift + rng.normal(0, 0.01, n))),
                      index=pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC"))
    df = pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]),
                       "high": close * 1.01, "low": close * 0.99,
                       "close": close, "volume": rng.uniform(1e5, 1e6, n)})

    res = run_ml_meta(df, model_id="logistic_regression", n_splits=4,
                      max_horizon=10, mom_lookback=20)
    sig = res.signal
    # Long-only, probability-sized: weights in [0, 1], nonzero only above gate.
    assert float(sig.min()) >= 0.0 and float(sig.max()) <= 1.0
    nz = sig[sig > 0]
    assert len(nz) > 0 and (nz > 0.55).all()
    # Nonzero bars must be momentum-long bars (direction comes from the primary).
    mom = close / close.shift(20) - 1.0
    assert (mom.loc[nz.index] > 0).all()
    assert res.model_id == "meta_logistic_regression"
    assert 0.0 <= res.oos_accuracy <= 1.0
    assert res.baseline_oos_accuracy is not None      # primary's raw win rate
    assert res.brier_score is not None
    assert all("taken_frac" in f for f in res.fold_metrics)


def test_meta_refuses_thin_samples():
    from app.ml.model import run_ml_meta

    rng = np.random.default_rng(5)
    n = 80
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))),
                      index=pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC"))
    df = pd.DataFrame({"open": close, "high": close, "low": close,
                       "close": close, "volume": np.full(n, 1e5)})
    with pytest.raises(ValueError, match="not enough momentum-long samples"):
        run_ml_meta(df, n_splits=5)


def test_meta_momentum_runs_through_the_runner():
    """Full path: QUEUED row -> run_and_persist -> completed, with meta
    diagnostics + the trial-counted DSR research key."""
    import uuid
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete

    from app.backtest.runner import run_and_persist
    from app.db.session import SessionLocal
    from app.models import (
        Asset, Backtest, BacktestYearlyResult, MlTrial, OhlcvBar, Strategy,
        StrategyVersion, User,
    )
    from app.models.enums import AssetClass, BacktestStatus, Timeframe

    rng = np.random.default_rng(8)
    n = 500
    drift = np.where(np.arange(n) % 150 < 90, 0.0015, -0.001)
    closes = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.01, n)))

    with SessionLocal() as db:
        u = User(email=f"meta_{uuid.uuid4().hex[:8]}@e.com",
                 username=f"meta_{uuid.uuid4().hex[:8]}", hashed_password="x")
        a = Asset(symbol=f"MT{uuid.uuid4().hex[:5]}", exchange="X",
                  asset_class=AssetClass.CRYPTO)
        db.add_all([u, a]); db.commit(); db.refresh(u); db.refresh(a)
        t0 = datetime(2021, 1, 1, tzinfo=timezone.utc)
        db.add_all([OhlcvBar(asset_id=a.id, timeframe=Timeframe.D1,
                             time=t0 + timedelta(days=i), open=float(closes[i]),
                             high=float(closes[i] * 1.01), low=float(closes[i] * 0.99),
                             close=float(closes[i]), volume=1e6) for i in range(n)])
        s = Strategy(user_id=u.id, name="meta"); db.add(s); db.flush()
        sv = StrategyVersion(strategy_id=s.id, version=1, code=""); db.add(sv)
        db.commit()
        bt = Backtest(user_id=u.id, strategy_version_id=sv.id,
                      status=BacktestStatus.QUEUED,
                      config={"asset_id": a.id, "timeframe": "1d",
                              "strategy": "ml_meta_momentum",
                              "params": {"n_splits": 4},
                              "initial_capital": 100000, "commission_bps": 5})
        db.add(bt); db.commit(); db.refresh(bt)
        ids = {"u": u.id, "a": a.id, "s": s.id, "bt": bt.id}

    try:
        run_and_persist(ids["bt"])
        with SessionLocal() as db:
            bt = db.get(Backtest, ids["bt"])
            assert bt.status == BacktestStatus.COMPLETED
            d = bt.diagnostics
            assert d["model_id"] == "meta_xgboost"
            assert d["n_trials"] >= 1                  # research-keyed DSR count
            assert d["baseline_oos_accuracy"] is not None
            assert "risk" in d                         # tail-risk block rides along
    finally:
        with SessionLocal() as db:
            db.execute(delete(BacktestYearlyResult).where(
                BacktestYearlyResult.backtest_id == ids["bt"]))
            db.execute(delete(Backtest).where(Backtest.id == ids["bt"]))
            db.execute(delete(MlTrial).where(
                MlTrial.research_key == f"meta_momentum:{ids['a']}"))
            db.execute(delete(StrategyVersion).where(StrategyVersion.strategy_id == ids["s"]))
            db.execute(delete(Strategy).where(Strategy.id == ids["s"]))
            db.execute(delete(OhlcvBar).where(OhlcvBar.asset_id == ids["a"]))
            db.execute(delete(Asset).where(Asset.id == ids["a"]))
            db.execute(delete(User).where(User.id == ids["u"]))
            db.commit()
