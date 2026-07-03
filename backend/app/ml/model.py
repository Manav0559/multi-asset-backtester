"""XGBoost direction-classifier strategy.

Trains a gradient-boosted classifier to predict whether the next
`horizon`-bar return is positive, using only backward-looking features, and
generates positions purely OUT-OF-SAMPLE via walk-forward CV with embargo.

Why this is honest (and interview-defensible):
  * Every prediction that becomes a trading signal comes from a model that
    never saw that bar during training (walk-forward).
  * The embargo (default = label horizon) purges the leakage between a
    training label's forward window and the test block.
  * The resulting signal feeds the SAME vectorized engine as every other
    strategy, whose `.shift(1)` applies it on the next bar — so there is no
    lookahead at signal-application time either.

`ml_direction_strategy(...)` returns a plain Strategy callable, so an ML
model is a drop-in wherever a built-in strategy is used (runner, engine,
API), with no special-casing downstream.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.backtest.strategies import Strategy
from app.ml.features import FEATURE_COLUMNS, build_features
from app.ml.labels import forward_return_label
from app.ml.validation import walk_forward_splits


@dataclass
class MLResult:
    signal: pd.Series                 # OOS positions in {0,1}, aligned to df index
    oos_accuracy: float               # directional accuracy on held-out folds
    n_predictions: int
    feature_importance: dict = field(default_factory=dict)


def _make_model(params: dict | None):
    from xgboost import XGBClassifier

    defaults = dict(
        n_estimators=120, max_depth=3, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0,
        random_state=42, n_jobs=1, eval_metric="logloss",
    )
    defaults.update(params or {})
    return XGBClassifier(**defaults)


def run_ml_direction(
    df: pd.DataFrame,
    *,
    horizon: int = 1,
    threshold: float = 0.0,
    n_splits: int = 5,
    embargo: int | None = None,
    prob_threshold: float = 0.5,
    model_params: dict | None = None,
) -> MLResult:
    """Walk-forward train/predict; return an OOS signal + diagnostics."""
    embargo = horizon if embargo is None else embargo

    feats = build_features(df)
    labels = forward_return_label(df["close"].astype(float), horizon, threshold)

    # Align features and labels on their common index (drops warmup + tail).
    common = feats.index.intersection(labels.index)
    X = feats.loc[common, FEATURE_COLUMNS]
    y = labels.loc[common]

    if len(X) < (n_splits + 1) * 10:
        raise ValueError(f"not enough samples ({len(X)}) for {n_splits}-fold walk-forward")

    signal = pd.Series(0.0, index=df.index)
    importances = np.zeros(len(FEATURE_COLUMNS))
    n_imp = 0
    correct = total = 0

    for train_idx, test_idx in walk_forward_splits(len(X), n_splits, embargo):
        model = _make_model(model_params)
        model.fit(X.iloc[train_idx], y.iloc[train_idx])

        proba = model.predict_proba(X.iloc[test_idx])[:, 1]
        preds = (proba > prob_threshold).astype(float)

        test_ts = X.index[test_idx]
        signal.loc[test_ts] = preds

        y_true = y.iloc[test_idx].to_numpy()
        correct += int(((proba > 0.5).astype(int) == y_true).sum())
        total += len(y_true)
        importances += model.feature_importances_
        n_imp += 1

    oos_acc = correct / total if total else float("nan")
    imp = (importances / n_imp) if n_imp else importances
    return MLResult(
        signal=signal,
        oos_accuracy=oos_acc,
        n_predictions=total,
        feature_importance=dict(sorted(
            zip(FEATURE_COLUMNS, imp.tolist()), key=lambda kv: kv[1], reverse=True)),
    )


def ml_direction_strategy(
    horizon: int = 1, threshold: float = 0.0, n_splits: int = 5,
    embargo: int | None = None, prob_threshold: float = 0.5,
    model_params: dict | None = None,
) -> Strategy:
    """Factory returning a Strategy that runs walk-forward ML internally.

    Note: this trains N models each time it's called, so it's heavier than a
    rule-based strategy — appropriate for the Celery worker, not a hot path."""
    def _strategy(df: pd.DataFrame) -> pd.Series:
        result = run_ml_direction(
            df, horizon=horizon, threshold=threshold, n_splits=n_splits,
            embargo=embargo, prob_threshold=prob_threshold, model_params=model_params)
        return result.signal

    return _strategy
