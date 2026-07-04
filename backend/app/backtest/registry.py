"""StrategyRegistry — single source of truth for every runnable strategy.

Unifies three shapes behind one lookup:
  - legacy factory functions (strategies.py, portfolio_strategies.py)
  - BaseStrategy subclasses (classic.py — the canonical shape going forward)
  - the BYOC sandbox (registered as `custom_code`; params carry the user source)

The runner dispatches on `kind` ("single" -> long/flat engine, "portfolio" ->
weight engine) instead of probing module dicts, and GET /strategies serves
`catalog()` so the frontend never hardcodes a strategy list again.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Callable

from app.backtest.base import BaseStrategy
from app.backtest.classic import CLASSIC_STRATEGIES
from app.backtest.portfolio_strategies import PORTFOLIO_STRATEGIES
from app.backtest.strategies import BUILTIN_STRATEGIES

_SCALAR_TYPES = (int, float, bool, str)


@dataclass(frozen=True)
class StrategyEntry:
    key: str
    kind: str                    # "single" | "portfolio"
    factory: Callable            # (**params) -> callable strategy
    description: str = ""
    category: str = "classic"
    defaults: dict = field(default_factory=dict)

    def build(self, params: dict | None):
        return self.factory(**(params or {}))


def _defaults_of(fn: Callable) -> dict:
    """Tunable params (name -> default) from a factory signature or class
    __init__ — feeds the frontend's dynamic param form."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {}
    out = {}
    for p in sig.parameters.values():
        if p.name in ("self", "assets") or p.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        default = None if p.default is inspect.Parameter.empty else p.default
        if default is None or isinstance(default, _SCALAR_TYPES):
            out[p.name] = default
    return out


class StrategyRegistry:
    def __init__(self):
        self._entries: dict[str, StrategyEntry] = {}

    def register(self, entry: StrategyEntry) -> None:
        if entry.key in self._entries:
            raise ValueError(f"duplicate strategy key '{entry.key}'")
        self._entries[entry.key] = entry

    def register_class(self, cls: type[BaseStrategy]) -> None:
        self.register(StrategyEntry(
            key=cls.key, kind=cls.kind, factory=cls,
            description=cls.description, category=cls.category,
            defaults=_defaults_of(cls.__init__),
        ))

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def get(self, key: str) -> StrategyEntry:
        if key not in self._entries:
            raise KeyError(f"unknown strategy '{key}'")
        return self._entries[key]

    def kind(self, key: str) -> str:
        return self.get(key).kind

    def build(self, key: str, params: dict | None):
        return self.get(key).build(params)

    def catalog(self) -> list[dict]:
        return [
            {"key": e.key, "kind": e.kind, "category": e.category,
             "description": e.description, "defaults": e.defaults}
            for e in sorted(self._entries.values(), key=lambda e: (e.kind, e.key))
        ]


_DESCRIPTIONS = {
    "sma_crossover": ("trend", "Long when the fast SMA is above the slow SMA"),
    "rsi_reversion": ("mean_reversion", "Long on RSI oversold, exit on overbought"),
    "buy_and_hold": ("baseline", "Always long — the benchmark"),
    "ml_direction": ("ml", "XGBoost direction classifier, walk-forward out-of-sample"),
    "long_short_sma": ("trend", "±1 long/short on the SMA cross — profits both ways"),
    "cross_sectional_momentum": ("arbitrage", "Dollar-neutral: long winners, short losers by trailing return"),
    "equal_weight_long": ("baseline", "Equal-weight long basket — the naive index"),
}


def _build_default_registry() -> StrategyRegistry:
    reg = StrategyRegistry()
    for key, factory in BUILTIN_STRATEGIES.items():
        cat, desc = _DESCRIPTIONS.get(key, ("classic", ""))
        reg.register(StrategyEntry(key=key, kind="single", factory=factory,
                                   description=desc, category=cat,
                                   defaults=_defaults_of(factory)))
    for key, factory in PORTFOLIO_STRATEGIES.items():
        cat, desc = _DESCRIPTIONS.get(key, ("classic", ""))
        reg.register(StrategyEntry(key=key, kind="portfolio", factory=factory,
                                   description=desc, category=cat,
                                   defaults=_defaults_of(factory)))
    for cls in CLASSIC_STRATEGIES:
        reg.register_class(cls)

    # ML model catalog: each sklearn/xgboost family is a selectable strategy
    # (`ml_<family>`) routed through the SAME purged+embargoed+calibrated
    # pipeline. ml_direction stays as the xgboost default (backward compat).
    from app.ml.model import MODEL_FAMILIES, ml_direction_strategy
    for fam, label in MODEL_FAMILIES.items():
        reg.register(StrategyEntry(
            key=f"ml_{fam}", kind="single",
            factory=lambda model_id=fam, **p: ml_direction_strategy(model_id=model_id, **p),
            description=f"{label} — walk-forward OOS, calibrated, vs logistic baseline",
            category="ml",
            defaults={"horizon": 1, "n_splits": 5, "embargo": 0},
        ))
    return reg


STRATEGY_REGISTRY = _build_default_registry()
