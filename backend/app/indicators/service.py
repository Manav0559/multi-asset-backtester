"""IndicatorService — the platform's TradingView-equivalent indicator engine.

A thin, safe wrapper over pandas-ta (150+ indicators: momentum, overlap,
volatility, volume, trend, statistics, candles, cycles). One service is used
by every consumer — the charting API, the strategy engine's enrichment step,
and the BYOC sandbox — so an indicator value is computed exactly one way
everywhere.

Safety contract:
  - Only indicators present in pandas-ta's own Category registry are callable,
    and only by name — no attribute traversal from user input.
  - Params are validated against the indicator's real signature; unknown or
    non-scalar params are rejected (no kwargs smuggling into pandas-ta).
  - Ichimoku's forward span (senkou projection beyond the last bar) is
    DROPPED: it is future-dated output and must never reach a strategy frame
    (the no-lookahead invariant).
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field

import pandas as pd
import pandas_ta as ta


class IndicatorError(ValueError):
    """Bad indicator name or params — maps to a 4xx at the API layer."""


@dataclass(frozen=True)
class IndicatorSpec:
    name: str
    params: dict = field(default_factory=dict)


# Signature entries that are data inputs or plumbing, not user-tunable params.
_NON_PARAMS = {
    "open", "open_", "high", "low", "close", "volume", "kwargs",
    "talib", "offset", "anchor",  # anchor: vwap takes a pandas offset alias, not numeric
}
_SCALAR_TYPES = (int, float, bool, str)


def _indicator_names() -> dict[str, str]:
    """{indicator_name: category} from pandas-ta's own registry."""
    names: dict[str, str] = {}
    for category, members in ta.Category.items():
        for name in members:
            if callable(getattr(ta, name, None)):
                names[name] = category
    return names


class IndicatorService:
    """Stateless: instances are cheap, the catalog is class-level."""

    _NAMES: dict[str, str] = _indicator_names()

    # ---------------------------------------------------------- catalog --
    @classmethod
    def catalog(cls) -> list[dict]:
        """Every supported indicator with its category and tunable params
        (name + default), for the frontend's indicator picker."""
        out = []
        for name, category in sorted(cls._NAMES.items()):
            fn = getattr(ta, name)
            params = []
            try:
                sig = inspect.signature(fn)
            except (TypeError, ValueError):
                sig = None
            if sig is not None:
                for p in sig.parameters.values():
                    if p.name in _NON_PARAMS or p.kind is inspect.Parameter.VAR_KEYWORD:
                        continue
                    default = None if p.default is inspect.Parameter.empty else p.default
                    if default is not None and not isinstance(default, _SCALAR_TYPES):
                        continue
                    params.append({"name": p.name, "default": default})
            out.append({"name": name, "category": category, "params": params})
        return out

    # ---------------------------------------------------------- compute --
    @classmethod
    def _validate(cls, spec: IndicatorSpec) -> None:
        if spec.name not in cls._NAMES:
            raise IndicatorError(f"unknown indicator '{spec.name}'")
        sig = inspect.signature(getattr(ta, spec.name))
        allowed = {p.name for p in sig.parameters.values()
                   if p.name not in _NON_PARAMS and p.kind is not inspect.Parameter.VAR_KEYWORD}
        for k, v in spec.params.items():
            if k not in allowed:
                raise IndicatorError(f"{spec.name}: unknown param '{k}'")
            if not isinstance(v, _SCALAR_TYPES):
                raise IndicatorError(f"{spec.name}: param '{k}' must be a scalar")

    @classmethod
    def compute(cls, df: pd.DataFrame, specs: list[IndicatorSpec]) -> pd.DataFrame:
        """Indicator columns for `df` (an OHLCV frame with a DatetimeIndex),
        aligned to df.index. Multi-output indicators (macd, bbands, ...)
        contribute one column per output, named by pandas-ta (MACD_12_26_9)."""
        result = pd.DataFrame(index=df.index)
        for spec in specs:
            cls._validate(spec)
            try:
                out = getattr(df.ta, spec.name)(**spec.params)
            except Exception as exc:  # pandas-ta raises bare Exceptions on bad windows
                raise IndicatorError(f"{spec.name}: {exc}") from exc
            if isinstance(out, tuple):  # ichimoku: (historical, forward span)
                out = out[0]
            if out is None:
                raise IndicatorError(f"{spec.name}: produced no output for this data")
            if isinstance(out, pd.Series):
                out = out.to_frame(name=out.name or spec.name.upper())
            # reindex, never join: forward-dated rows must not extend the frame
            result = result.join(out.reindex(df.index))
        return result

    @classmethod
    def enrich(cls, df: pd.DataFrame, specs: list[IndicatorSpec]) -> pd.DataFrame:
        """`df` plus indicator columns — the frame handed to strategies."""
        if not specs:
            return df
        return df.join(cls.compute(df, specs))

    # ------------------------------------------------------------ parse --
    @staticmethod
    def parse_spec(raw: str) -> list[IndicatorSpec]:
        """Parse the compact query form `rsi:length=14;macd:fast=12,slow=26`.
        Values are coerced int -> float -> bool -> str."""
        specs: list[IndicatorSpec] = []
        for chunk in filter(None, (c.strip() for c in raw.split(";"))):
            name, _, param_str = chunk.partition(":")
            params: dict = {}
            for pair in filter(None, (p.strip() for p in param_str.split(","))):
                k, sep, v = pair.partition("=")
                if not sep:
                    raise IndicatorError(f"bad param '{pair}' (expected key=value)")
                params[k.strip()] = _coerce(v.strip())
            specs.append(IndicatorSpec(name=name.strip().lower(), params=params))
        return specs


def _coerce(v: str):
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return v
