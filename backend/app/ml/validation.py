"""Walk-forward cross-validation with an embargo (purged CV).

Standard k-fold shuffles rows and is INVALID for time series: it trains on
the future to predict the past, and overlapping feature/label windows leak
information across the split. This module implements the correct scheme
(López de Prado, *Advances in Financial ML*):

  * Walk-forward: folds move forward in time. Each test block is a
    contiguous future slice; training uses only data strictly before it.
  * Embargo: a gap of `embargo` bars is PURGED between the end of the
    training window and the start of the test window. Because a label at
    time t looks `horizon` bars ahead, a training sample right before the
    test set would otherwise share information with it. Setting
    embargo >= label horizon removes that overlap.

`walk_forward_splits` yields (train_idx, test_idx) integer-position arrays,
mirroring the sklearn splitter protocol so it drops into existing loops.
"""
from __future__ import annotations

import numpy as np


def walk_forward_splits(n_samples: int, n_splits: int = 5, embargo: int = 0,
                        purge: int = 0, min_train: int | None = None):
    """Yield (train_idx, test_idx) for expanding-window walk-forward CV.

    Two DISTINCT gaps sit between the end of training and the start of a test
    block (López de Prado, AFML ch.7):

      * PURGE — drops the last `purge` training samples whose forward LABEL
        window overlaps the test block. A label at time t peeks `horizon` bars
        ahead, so a training sample within `horizon` of the test start shares
        its outcome with the test set. `purge >= label_horizon` removes it.
        This is the leakage that inflates OOS scores if you forget it.
      * EMBARGO — an ADDITIONAL buffer (serial-correlation guard) on top of the
        purge.

    The total gap is `purge + embargo`. Purging is the correctness-critical
    one; a fold-hygiene test asserts train/test never overlap and the gap holds.
    """
    if n_splits < 1:
        raise ValueError("n_splits must be >= 1")
    if embargo < 0 or purge < 0:
        raise ValueError("embargo and purge must be >= 0")

    gap = purge + embargo
    initial_train = min_train if min_train is not None else n_samples // (n_splits + 1)
    if initial_train < 1 or initial_train >= n_samples:
        raise ValueError("min_train too small/large for the data")

    test_region = n_samples - initial_train
    fold_size = test_region // n_splits
    if fold_size < 1:
        raise ValueError("not enough samples for the requested n_splits")

    for k in range(n_splits):
        test_start = initial_train + k * fold_size
        test_end = n_samples if k == n_splits - 1 else test_start + fold_size
        train_end = max(test_start - gap, 0)   # purge + embargo bars removed
        if train_end < 1:
            continue
        train_idx = np.arange(0, train_end)
        test_idx = np.arange(test_start, test_end)
        if len(test_idx) == 0:
            continue
        yield train_idx, test_idx
