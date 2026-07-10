"""Factor attribution: the regression must RECOVER known loadings, and the
factor construction must be lookahead-free by construction."""
import numpy as np
import pandas as pd

from app.backtest.attribution import attribute, build_factors


def _idx(n):
    return pd.date_range("2022-01-01", periods=n, freq="D")


def test_ols_recovers_known_betas_and_alpha():
    rng = np.random.default_rng(3)
    n = 2000
    f = pd.DataFrame({
        "MKT": rng.normal(0.0003, 0.01, n),
        "MOM": rng.normal(0.0001, 0.006, n),
        "LIQ": rng.normal(0.0, 0.004, n),
    }, index=_idx(n))
    alpha_per_bar = 0.0002
    r = (alpha_per_bar + 0.8 * f["MKT"] - 0.4 * f["MOM"] + 0.15 * f["LIQ"]
         + rng.normal(0, 0.001, n))
    out = attribute(pd.Series(r, index=f.index), f)
    assert abs(out["betas"]["MKT"] - 0.8) < 0.02
    assert abs(out["betas"]["MOM"] + 0.4) < 0.02
    assert abs(out["betas"]["LIQ"] - 0.15) < 0.03
    # annualized alpha ~ 0.0002 * 252 = 5.04%
    assert abs(out["alpha_annual_pct"] - 5.04) < 1.0
    assert out["r_squared"] > 0.95
    assert out["n_obs"] == n


def test_pure_market_portfolio_has_no_alpha():
    rng = np.random.default_rng(5)
    n = 1500
    f = pd.DataFrame({"MKT": rng.normal(0.0004, 0.012, n),
                      "MOM": rng.normal(0, 0.005, n),
                      "LIQ": rng.normal(0, 0.004, n)}, index=_idx(n))
    out = attribute(f["MKT"].rename("r"), f)  # the strategy IS the market
    assert abs(out["betas"]["MKT"] - 1.0) < 1e-6
    assert abs(out["alpha_annual_pct"]) < 1e-6
    assert out["r_squared"] > 0.999999


def test_attribute_refuses_thin_samples():
    f = pd.DataFrame({"MKT": [0.01] * 30, "MOM": [0.0] * 30, "LIQ": [0.0] * 30},
                     index=_idx(30))
    assert attribute(f["MKT"], f) is None  # < MIN_OBS aligned


def test_build_factors_is_lookahead_free():
    """Truncation invariance: factors computed on the full panel must equal
    factors computed on a truncated panel, over the shared index — the same
    invariance every strategy in the registry is held to."""
    rng = np.random.default_rng(9)
    n, m = 400, 15
    closes = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0, 0.01, (n, m)), axis=0)),
        index=_idx(n), columns=[f"a{i}" for i in range(m)])
    volumes = pd.DataFrame(rng.uniform(1e5, 1e6, (n, m)),
                           index=closes.index, columns=closes.columns)
    full = build_factors(closes, volumes)
    trunc = build_factors(closes.iloc[:300], volumes.iloc[:300])
    shared = trunc.index
    pd.testing.assert_frame_equal(full.loc[shared], trunc, atol=1e-12, rtol=0)


def test_build_factors_against_real_universe(client):
    """Integration: factors build from the ACTUAL stored NASDAQ universe via
    the runner's loader path — finite values, sane magnitudes."""
    from datetime import datetime, timezone

    from app.backtest.runner import load_bars
    from app.db.session import SessionLocal
    from app.models import Asset
    from app.models.enums import AssetClass, Timeframe
    from sqlalchemy import select

    with SessionLocal() as db:
        ids = db.scalars(select(Asset.id).where(
            Asset.asset_class == AssetClass.US_EQUITY)).all()[:30]
        closes, volumes = {}, {}
        for aid in ids:
            bars = load_bars(db, aid, Timeframe.D1,
                             datetime(2024, 1, 1, tzinfo=timezone.utc),
                             datetime(2025, 6, 1, tzinfo=timezone.utc))
            if len(bars) >= 60:
                closes[str(aid)] = bars["close"]
                volumes[str(aid)] = bars["volume"]
    if len(closes) < 10:  # dev DB without the universe: skip honestly
        import pytest
        pytest.skip("universe not backfilled in this database")
    f = build_factors(pd.DataFrame(closes), pd.DataFrame(volumes))
    assert set(f.columns) == {"MKT", "MOM", "LIQ"}
    assert len(f) > 100 and np.isfinite(f.to_numpy()).all()
    # daily factor returns live in sane bounds (not percent/fraction mixups)
    assert f.abs().max().max() < 0.5
