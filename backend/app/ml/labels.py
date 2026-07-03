"""Label construction for supervised strategies.

The label is the ONLY forward-looking quantity in the ML pipeline, and it
is isolated here on purpose. `forward_return_label` marks whether the
`horizon`-bar-ahead return exceeds a threshold — a binary up/not-up target.

Because the label at bar t peeks `horizon` bars into the future, the last
`horizon` rows have no valid label and are dropped. When these labels are
used with walk-forward CV, the embargo (>= horizon) prevents a training
label's lookahead window from overlapping the test set.
"""
from __future__ import annotations

import pandas as pd


def forward_return(close: pd.Series, horizon: int = 1) -> pd.Series:
    return close.shift(-horizon) / close - 1.0


def forward_return_label(close: pd.Series, horizon: int = 1,
                         threshold: float = 0.0) -> pd.Series:
    """1 if the forward `horizon`-bar return > threshold, else 0.
    Trailing rows without a full forward window are dropped."""
    fwd = forward_return(close, horizon)
    label = (fwd > threshold).astype(int)
    label[fwd.isna()] = pd.NA
    return label.dropna().astype(int)
