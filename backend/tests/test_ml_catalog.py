"""ML catalog: purge vs embargo fold hygiene, and every sklearn family runs
through the same leakage-hardened pipeline (calibrated OOS + logistic baseline).
"""
import numpy as np
import pandas as pd
import pytest

from app.ml.model import MODEL_FAMILIES, run_ml_direction
from app.ml.validation import walk_forward_splits


# ------------------------------------------------------------ fold hygiene --
def test_purge_and_embargo_create_the_gap():
    """purge + embargo bars separate train end from test start; no overlap."""
    for tr, te in walk_forward_splits(300, n_splits=3, embargo=2, purge=5):
        assert tr.max() < te.min(), "train/test overlap"
        gap = int(te.min()) - int(tr.max()) - 1
        assert gap >= 5 + 2, f"gap {gap} < purge+embargo"


def test_purge_bites_relative_to_no_purge():
    """With purge=0 training is ADJACENT to the test block (would leak an
    h-bar label); purge=h opens a >= h gap. This is the leakage purge removes."""
    adj = [int(te.min()) - int(tr.max()) - 1
           for tr, te in walk_forward_splits(300, 3, embargo=0, purge=0)]
    purged = [int(te.min()) - int(tr.max()) - 1
              for tr, te in walk_forward_splits(300, 3, embargo=0, purge=10)]
    assert min(adj) == 0            # adjacent — the leak
    assert min(purged) >= 10        # purged — closed


# ------------------------------------------------------- per-family runs --
def _synth(n=500, seed=1):
    idx = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
    rng = np.random.default_rng(seed)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))), index=idx)
    return pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close * 1.01, "low": close * 0.99, "close": close,
        "volume": rng.uniform(1e5, 1e6, n),
    }, index=idx)


@pytest.mark.parametrize("family", list(MODEL_FAMILIES))
def test_family_runs_calibrated_with_baseline(family):
    df = _synth()
    res = run_ml_direction(df, model_id=family, n_splits=3, horizon=1)
    # Produces an OOS signal in {0,1}.
    assert set(np.unique(res.signal)) <= {0.0, 1.0}
    assert res.n_predictions > 50
    assert res.model_id == family
    # Calibrated probabilities => a Brier score; a logistic baseline for comparison.
    assert res.brier_score is not None and 0 <= res.brier_score <= 1
    assert res.baseline_oos_accuracy is not None
    # Fold-wise IS/OOS recorded for honesty (IS should not be < OOS by luck alone).
    assert len(res.fold_metrics) >= 1
    assert all("is_acc" in f and "oos_acc" in f for f in res.fold_metrics)
    # Honest OOS: near coin-flip on noise, never implausibly high.
    assert 0.3 <= res.oos_accuracy <= 0.75


def test_unknown_family_rejected():
    with pytest.raises(ValueError, match="unknown model family"):
        run_ml_direction(_synth(120), model_id="transformer", n_splits=2)
