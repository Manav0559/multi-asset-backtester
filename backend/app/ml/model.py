"""Direction-classifier strategy — a catalog of sklearn-compatible model
families behind ONE leakage-hardened pipeline.

Every family predicts whether the next `horizon`-bar return is positive, using
only backward-looking features, and generates positions purely OUT-OF-SAMPLE
via purged + embargoed walk-forward CV. The pipeline is identical across
families, so swapping RandomForest for XGBoost changes only the estimator —
never the honesty guarantees:

  * purged (>= label horizon) + embargoed walk-forward CV — no label leakage.
  * isotonic probability calibration fit on a fold-INTERNAL split (never the
    test block), so the p>0.5 threshold means what it says (Brier reported).
  * a LogisticRegression baseline on the same folds — "did the fancy model beat
    the dumb one?" is answered on every report.
  * the resulting signal feeds the SAME vectorized engine (`.shift(1)`), so no
    lookahead at application time either.

Allowed families are sklearn-only (+ xgboost, already a dep) — no torch. See
MODEL_FAMILIES.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.backtest.strategies import Strategy
from app.ml.features import FEATURE_COLUMNS, build_features
from app.ml.labels import forward_return_label
from app.ml.validation import walk_forward_splits

# family id -> (label, factory). All expose predict_proba. sklearn + xgboost
# only (no torch/lightgbm/catboost — see the run's kill list).
MODEL_FAMILIES: dict[str, str] = {
    "logistic_regression": "Logistic Regression (linear baseline)",
    "decision_tree": "Decision Tree",
    "random_forest": "Random Forest",
    "extra_trees": "Extra Trees",
    "gradient_boosting": "Gradient Boosting (sklearn)",
    "mlp": "Neural Net (sklearn MLP)",
    "xgboost": "XGBoost",
}


def _make_model(model_id: str, params: dict | None):
    p = params or {}
    if model_id == "logistic_regression":
        from sklearn.linear_model import LogisticRegression
        return LogisticRegression(max_iter=500, C=p.get("C", 1.0))
    if model_id == "decision_tree":
        from sklearn.tree import DecisionTreeClassifier
        return DecisionTreeClassifier(max_depth=p.get("max_depth", 4), random_state=42)
    if model_id == "random_forest":
        from sklearn.ensemble import RandomForestClassifier
        return RandomForestClassifier(
            n_estimators=p.get("n_estimators", 120), max_depth=p.get("max_depth", 5),
            n_jobs=1, random_state=42)
    if model_id == "extra_trees":
        from sklearn.ensemble import ExtraTreesClassifier
        return ExtraTreesClassifier(
            n_estimators=p.get("n_estimators", 120), max_depth=p.get("max_depth", 5),
            n_jobs=1, random_state=42)
    if model_id == "gradient_boosting":
        from sklearn.ensemble import GradientBoostingClassifier
        return GradientBoostingClassifier(
            n_estimators=p.get("n_estimators", 100), max_depth=p.get("max_depth", 3),
            learning_rate=p.get("learning_rate", 0.05), random_state=42)
    if model_id == "mlp":
        from sklearn.neural_network import MLPClassifier
        return MLPClassifier(hidden_layer_sizes=p.get("hidden", (32, 16)),
                             max_iter=p.get("max_iter", 300), random_state=42)
    if model_id == "xgboost":
        from xgboost import XGBClassifier
        return XGBClassifier(
            n_estimators=p.get("n_estimators", 120), max_depth=p.get("max_depth", 3),
            learning_rate=p.get("learning_rate", 0.05), subsample=0.8,
            colsample_bytree=0.8, reg_lambda=1.0, random_state=42, n_jobs=1,
            eval_metric="logloss")
    raise ValueError(f"unknown model family '{model_id}' (allowed: {list(MODEL_FAMILIES)})")


def _importance(model) -> np.ndarray | None:
    if hasattr(model, "feature_importances_"):
        return np.asarray(model.feature_importances_, dtype=float)
    if hasattr(model, "coef_"):
        return np.abs(np.asarray(model.coef_, dtype=float)).ravel()
    return None


@dataclass
class MLResult:
    signal: pd.Series
    oos_accuracy: float
    n_predictions: int
    model_id: str = "xgboost"
    brier_score: float | None = None            # calibration quality on OOS probs
    baseline_oos_accuracy: float | None = None  # logistic baseline, same folds
    fold_metrics: list = field(default_factory=list)  # [{fold,is_acc,oos_acc}]
    feature_importance: dict = field(default_factory=dict)      # mean
    feature_importance_std: dict = field(default_factory=dict)  # across folds


def _fit_predict_proba(model_id, params, Xtr, ytr, Xte, calibrate: bool):
    """Fit on the training fold (optionally isotonic-calibrated on a
    fold-internal split) and return OOS probabilities for the test block."""
    from sklearn.calibration import CalibratedClassifierCV
    model = _make_model(model_id, params)
    if calibrate and len(Xtr) >= 60 and ytr.nunique() > 1:
        # cv=3 is INTERNAL to the training fold — the test block is never seen.
        try:
            cal = CalibratedClassifierCV(model, method="isotonic", cv=3)
            cal.fit(Xtr, ytr)
            return cal.predict_proba(Xte)[:, 1], model.fit(Xtr, ytr)
        except Exception:  # noqa: BLE001 — degenerate fold; fall back to raw
            pass
    model.fit(Xtr, ytr)
    return model.predict_proba(Xte)[:, 1], model


def run_ml_direction(
    df: pd.DataFrame,
    *,
    model_id: str = "xgboost",
    horizon: int = 1,
    threshold: float = 0.0,
    n_splits: int = 5,
    embargo: int | None = None,
    prob_threshold: float = 0.5,
    calibrate: bool = True,
    model_params: dict | None = None,
) -> MLResult:
    """Walk-forward train/predict for one model family; OOS signal + diagnostics."""
    if model_id not in MODEL_FAMILIES:
        raise ValueError(f"unknown model family '{model_id}'")
    embargo = 0 if embargo is None else embargo

    feats = build_features(df)
    labels = forward_return_label(df["close"].astype(float), horizon, threshold)
    common = feats.index.intersection(labels.index)
    X = feats.loc[common, FEATURE_COLUMNS]
    y = labels.loc[common]
    if len(X) < (n_splits + 1) * 10:
        raise ValueError(f"not enough samples ({len(X)}) for {n_splits}-fold walk-forward")

    signal = pd.Series(0.0, index=df.index)
    imps: list[np.ndarray] = []
    fold_metrics: list[dict] = []
    all_true: list[np.ndarray] = []
    all_proba: list[np.ndarray] = []
    base_correct = base_total = 0

    # purge >= label horizon is the leakage-critical gap; embargo is extra buffer.
    for i, (tr, te) in enumerate(walk_forward_splits(len(X), n_splits, embargo=embargo, purge=horizon)):
        Xtr, ytr, Xte, yte = X.iloc[tr], y.iloc[tr], X.iloc[te], y.iloc[te]
        proba, fitted = _fit_predict_proba(model_id, model_params, Xtr, ytr, Xte, calibrate)
        preds = (proba > prob_threshold).astype(float)
        signal.loc[X.index[te]] = preds

        yt = yte.to_numpy()
        oos_acc = float(((proba > 0.5).astype(int) == yt).mean()) if len(yt) else float("nan")
        is_pred = (fitted.predict_proba(Xtr)[:, 1] > 0.5).astype(int)
        is_acc = float((is_pred == ytr.to_numpy()).mean()) if len(ytr) else float("nan")
        fold_metrics.append({"fold": i, "is_acc": round(is_acc, 4), "oos_acc": round(oos_acc, 4)})
        all_true.append(yt); all_proba.append(proba)
        imp = _importance(fitted)
        if imp is not None and len(imp) == len(FEATURE_COLUMNS):
            imps.append(imp)

        # Logistic baseline on the same fold.
        base = _make_model("logistic_regression", None)
        base.fit(Xtr, ytr)
        base_correct += int((base.predict(Xte) == yt).sum())
        base_total += len(yt)

    true = np.concatenate(all_true) if all_true else np.array([])
    proba = np.concatenate(all_proba) if all_proba else np.array([])
    oos_acc = float(((proba > 0.5).astype(int) == true).mean()) if len(true) else float("nan")
    brier = float(np.mean((proba - true) ** 2)) if len(true) else None
    base_acc = base_correct / base_total if base_total else None

    imp_mean = np.mean(imps, axis=0) if imps else np.zeros(len(FEATURE_COLUMNS))
    imp_std = np.std(imps, axis=0) if imps else np.zeros(len(FEATURE_COLUMNS))
    order = np.argsort(imp_mean)[::-1]
    return MLResult(
        signal=signal, oos_accuracy=oos_acc, n_predictions=int(len(true)),
        model_id=model_id, brier_score=brier, baseline_oos_accuracy=base_acc,
        fold_metrics=fold_metrics,
        feature_importance={FEATURE_COLUMNS[i]: round(float(imp_mean[i]), 4) for i in order},
        feature_importance_std={FEATURE_COLUMNS[i]: round(float(imp_std[i]), 4) for i in order},
    )


def ml_direction_strategy(model_id: str = "xgboost", horizon: int = 1,
                          threshold: float = 0.0, n_splits: int = 5,
                          embargo: int | None = None, prob_threshold: float = 0.5,
                          model_params: dict | None = None) -> Strategy:
    def _strategy(df: pd.DataFrame) -> pd.Series:
        return run_ml_direction(
            df, model_id=model_id, horizon=horizon, threshold=threshold,
            n_splits=n_splits, embargo=embargo, prob_threshold=prob_threshold,
            model_params=model_params).signal
    return _strategy


def run_ml_meta(
    df: pd.DataFrame,
    *,
    model_id: str = "xgboost",
    pt_mult: float = 2.0,
    sl_mult: float = 1.0,
    max_horizon: int = 10,
    mom_lookback: int = 20,
    n_splits: int = 5,
    embargo: int | None = None,
    prob_threshold: float = 0.55,
    calibrate: bool = True,
    model_params: dict | None = None,
) -> MLResult:
    """Meta-labeling (López de Prado): a transparent PRIMARY rule decides
    direction; the model decides only WHETHER TO TAKE and HOW BIG.

    Primary: long when trailing `mom_lookback`-bar momentum is positive
    (long-only v1 — the single-asset engine is long/flat). Meta target:
    triple-barrier outcome of that long — did it hit profit-take before
    stop-loss? The classifier learns P(win | features); position size is the
    CALIBRATED probability itself (that's why calibration matters here: the
    probability IS the position), gated at `prob_threshold`.

    Leakage: barrier labels peek up to max_horizon bars ahead, so folds are
    purged by max_horizon. Rows are the momentum-long SUBSET of bars —
    purging k subset rows spans >= k real bars, so the purge is conservative.

    `baseline_oos_accuracy` is the primary's UNFILTERED OOS win rate — the
    number the meta model must beat to be adding anything.
    """
    from app.ml.labels import triple_barrier_label

    if model_id not in MODEL_FAMILIES:
        raise ValueError(f"unknown model family '{model_id}'")
    embargo = 0 if embargo is None else embargo

    close = df["close"].astype(float)
    momentum = close / close.shift(mom_lookback) - 1.0
    long_bars = momentum > 0                      # the primary's entry set

    bars = triple_barrier_label(close, pt_mult=pt_mult, sl_mult=sl_mult,
                                max_horizon=max_horizon)
    feats = build_features(df)
    rows = feats.index.intersection(bars.index[long_bars.reindex(bars.index,
                                                                 fill_value=False)])
    X = feats.loc[rows, FEATURE_COLUMNS]
    y = (bars.loc[rows, "label"] == 1).astype(int)   # win = PT before SL
    if len(X) < (n_splits + 1) * 10:
        raise ValueError(f"not enough momentum-long samples ({len(X)}) "
                         f"for {n_splits}-fold walk-forward")

    signal = pd.Series(0.0, index=df.index)
    imps: list[np.ndarray] = []
    fold_metrics: list[dict] = []
    all_true: list[np.ndarray] = []
    all_proba: list[np.ndarray] = []
    prim_wins = prim_total = 0

    for i, (tr, te) in enumerate(walk_forward_splits(
            len(X), n_splits, embargo=embargo, purge=max_horizon)):
        Xtr, ytr, Xte, yte = X.iloc[tr], y.iloc[tr], X.iloc[te], y.iloc[te]
        proba, fitted = _fit_predict_proba(model_id, model_params, Xtr, ytr, Xte, calibrate)
        take = proba > prob_threshold
        # Direction from the primary (long), size from calibrated confidence.
        signal.loc[X.index[te][take]] = proba[take]

        yt = yte.to_numpy()
        oos_acc = float(((proba > 0.5).astype(int) == yt).mean()) if len(yt) else float("nan")
        is_pred = (fitted.predict_proba(Xtr)[:, 1] > 0.5).astype(int)
        is_acc = float((is_pred == ytr.to_numpy()).mean()) if len(ytr) else float("nan")
        fold_metrics.append({"fold": i, "is_acc": round(is_acc, 4),
                             "oos_acc": round(oos_acc, 4),
                             "taken_frac": round(float(take.mean()), 4)})
        all_true.append(yt); all_proba.append(proba)
        prim_wins += int(yt.sum()); prim_total += len(yt)
        imp = _importance(fitted)
        if imp is not None and len(imp) == len(FEATURE_COLUMNS):
            imps.append(imp)

    true = np.concatenate(all_true) if all_true else np.array([])
    proba = np.concatenate(all_proba) if all_proba else np.array([])
    oos_acc = float(((proba > 0.5).astype(int) == true).mean()) if len(true) else float("nan")
    brier = float(np.mean((proba - true) ** 2)) if len(true) else None
    primary_win_rate = prim_wins / prim_total if prim_total else None

    imp_mean = np.mean(imps, axis=0) if imps else np.zeros(len(FEATURE_COLUMNS))
    imp_std = np.std(imps, axis=0) if imps else np.zeros(len(FEATURE_COLUMNS))
    order = np.argsort(imp_mean)[::-1]
    return MLResult(
        signal=signal, oos_accuracy=oos_acc, n_predictions=int(len(true)),
        model_id=f"meta_{model_id}", brier_score=brier,
        baseline_oos_accuracy=primary_win_rate,   # the unfiltered primary
        fold_metrics=fold_metrics,
        feature_importance={FEATURE_COLUMNS[i]: round(float(imp_mean[i]), 4) for i in order},
        feature_importance_std={FEATURE_COLUMNS[i]: round(float(imp_std[i]), 4) for i in order},
    )


def ml_meta_strategy(model_id: str = "xgboost", pt_mult: float = 2.0,
                     sl_mult: float = 1.0, max_horizon: int = 10,
                     mom_lookback: int = 20, n_splits: int = 5,
                     embargo: int | None = None, prob_threshold: float = 0.55,
                     model_params: dict | None = None) -> Strategy:
    def _strategy(df: pd.DataFrame) -> pd.Series:
        return run_ml_meta(
            df, model_id=model_id, pt_mult=pt_mult, sl_mult=sl_mult,
            max_horizon=max_horizon, mom_lookback=mom_lookback,
            n_splits=n_splits, embargo=embargo, prob_threshold=prob_threshold,
            model_params=model_params).signal
    return _strategy
