"""BYOC — "Bring Your Own Code" custom strategy sandbox.

Users submit a Python class extending CustomStrategy. The platform contract
(the ONLY thing users need to learn):

    class MyStrategy(CustomStrategy):
        params = {"fast": 10, "slow": 30}          # editable defaults

        def setup(self):
            # optional: vectorized precompute; self.data is the OHLCV frame,
            # self.indicator("rsi", length=14) gives any of the 150+ indicators
            self.fast = self.data["close"].rolling(self.params["fast"]).mean()

        def next(self, i, bar):
            # per-bar decision -> target weight in [-1, 1]
            # (0 = flat, 1 = fully long, -1 = fully short)
            return 1.0 if self.fast.iloc[i] > bar["close"] else 0.0

        # OR override generate() and return the whole weight Series at once —
        # vectorized, much faster, preferred for anything heavy.

`on_bar` is accepted as an alias of `next`; `on_tick` is reserved for live
paper-trading hooks and is a documented no-op in backtests. The engine applies
its usual signal.shift(1), so user code cannot trade on the current bar even
if it tries.

Defense-in-depth (this is NOT a hostile-multitenant-proof jail — the worker's
container, memory rlimit, and non-root user are the real fence):
  1. AST allowlist: no imports, no dunder/underscore access, no exec/eval/open,
     no global/nonlocal. Rejected before any code object is created.
  2. exec() under a curated builtins dict — pd/np/math are injected, nothing
     else is importable.
  3. Output is validated + clipped to [-1, 1] and reindexed to the data frame.
"""
from __future__ import annotations

import ast
import math

import numpy as np
import pandas as pd

from app.backtest.base import BaseStrategy
from app.indicators import IndicatorService, IndicatorSpec

MAX_CODE_BYTES = 64 * 1024

_FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "open", "input", "breakpoint", "__import__",
    "getattr", "setattr", "delattr", "vars", "globals", "locals", "memoryview",
    "exit", "quit", "help",
}

import builtins as _builtins  # noqa: E402

_SAFE_BUILTINS = {
    name: getattr(_builtins, name)
    for name in (
        "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter",
        "float", "frozenset", "int", "isinstance", "issubclass", "iter", "len",
        "list", "map", "max", "min", "next", "pow", "print", "range", "repr",
        "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple",
        "zip",
        # exceptions users legitimately raise/catch
        "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
        "ZeroDivisionError", "StopIteration", "ArithmeticError", "RuntimeError",
    )
    if hasattr(_builtins, name)
}
# `class` statements compile to a __build_class__ call — required, harmless.
_SAFE_BUILTINS["__build_class__"] = _builtins.__build_class__


class SandboxError(ValueError):
    """User code rejected or crashed — surfaced verbatim to the submitter."""


class CustomStrategy(BaseStrategy):
    """The base class user code extends. Never instantiated directly."""

    key = "custom_code"
    description = "User-submitted Python strategy (BYOC sandbox)"
    category = "custom"
    kind = "single"

    #: overridable class-level defaults, merged with per-run params
    params: dict = {}

    def __init__(self, data: pd.DataFrame | None = None, **params):
        merged = {**type(self).params, **params}
        super().__init__(**merged)
        self.params = merged
        self.data = data if data is not None else pd.DataFrame()

    # ------------------------------------------------------------ helpers --
    def indicator(self, name: str, **params) -> pd.DataFrame:
        """Any platform indicator, computed on self.data (backward-looking)."""
        return IndicatorService.compute(self.data, [IndicatorSpec(name, params)])

    # -------------------------------------------------------------- hooks --
    def setup(self) -> None:
        """Optional vectorized precompute before the bar loop."""

    def next(self, i: int, bar: pd.Series) -> float:
        """Per-bar decision: return the target weight in [-1, 1]."""
        raise NotImplementedError("override next(i, bar) or generate(data)")

    def on_tick(self, tick) -> None:
        """Reserved for live paper-trading; no-op in backtests."""

    # ------------------------------------------------------------- engine --
    def generate(self, data: pd.DataFrame) -> pd.Series:
        self.data = data
        self.setup()
        step = getattr(self, "on_bar", None) or self.next
        weights = np.zeros(len(data))
        for i, (_, bar) in enumerate(data.iterrows()):
            w = step(i, bar)
            weights[i] = 0.0 if w is None else float(w)
        return pd.Series(weights, index=data.index)


# ------------------------------------------------------------- validation --
class _Validator(ast.NodeVisitor):
    def __init__(self):
        self.errors: list[str] = []

    def _deny(self, node: ast.AST, msg: str) -> None:
        self.errors.append(f"line {getattr(node, 'lineno', '?')}: {msg}")

    def visit_Import(self, node):  # noqa: N802
        self._deny(node, "imports are not allowed — pd, np, and math are pre-loaded")

    def visit_ImportFrom(self, node):  # noqa: N802
        self._deny(node, "imports are not allowed — pd, np, and math are pre-loaded")

    def visit_Global(self, node):  # noqa: N802
        self._deny(node, "global statements are not allowed")

    def visit_Nonlocal(self, node):  # noqa: N802
        self._deny(node, "nonlocal statements are not allowed")

    def visit_Attribute(self, node):  # noqa: N802
        if node.attr.startswith("_"):
            self._deny(node, f"access to '{node.attr}' is not allowed")
        self.generic_visit(node)

    def visit_Name(self, node):  # noqa: N802
        if node.id.startswith("__") or node.id in _FORBIDDEN_CALLS:
            self._deny(node, f"'{node.id}' is not allowed in the sandbox")
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):  # noqa: N802
        self._deny(node, "async code is not allowed")


def validate_code(source: str) -> list[str]:
    """Static errors in user code; empty list means it passed the gate."""
    if len(source.encode()) > MAX_CODE_BYTES:
        return [f"source exceeds {MAX_CODE_BYTES // 1024}KB"]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"line {exc.lineno}: {exc.msg}"]
    v = _Validator()
    v.visit(tree)
    return v.errors


def build_custom_strategy(source: str, params: dict | None = None) -> CustomStrategy:
    """Validate, exec under restricted globals, and instantiate the single
    CustomStrategy subclass the user defined."""
    errors = validate_code(source)
    if errors:
        raise SandboxError("; ".join(errors))

    namespace: dict = {
        "__builtins__": _SAFE_BUILTINS,
        "__name__": "user_strategy",   # class creation reads the module name
        "CustomStrategy": CustomStrategy,
        "pd": pd, "np": np, "math": math,
    }
    try:
        exec(compile(source, "<user_strategy>", "exec"), namespace)  # noqa: S102
    except Exception as exc:  # noqa: BLE001 — user error, not ours
        raise SandboxError(f"{type(exc).__name__}: {exc}") from exc

    classes = [v for v in namespace.values()
               if isinstance(v, type) and issubclass(v, CustomStrategy) and v is not CustomStrategy]
    if len(classes) != 1:
        raise SandboxError(
            f"define exactly one class extending CustomStrategy (found {len(classes)})")
    try:
        return classes[0](**(params or {}))
    except Exception as exc:  # noqa: BLE001
        raise SandboxError(f"constructor failed: {type(exc).__name__}: {exc}") from exc


def run_custom_strategy(strategy: CustomStrategy, data: pd.DataFrame) -> pd.Series:
    """Execute + validate output: a numeric Series aligned to data, in [-1, 1]."""
    try:
        out = strategy.generate(data)
    except SandboxError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SandboxError(f"strategy raised {type(exc).__name__}: {exc}") from exc
    if not isinstance(out, pd.Series):
        raise SandboxError(f"generate() must return a pandas Series, got {type(out).__name__}")
    out = pd.to_numeric(out, errors="coerce").reindex(data.index).fillna(0.0)
    return out.clip(-1.0, 1.0)


#: shown in the frontend editor as the starting point
DEFAULT_TEMPLATE = '''\
class MyStrategy(CustomStrategy):
    """Golden cross with an RSI filter — edit freely.

    Contract: produce a target weight per bar in [-1, 1]
    (0 = flat, 1 = fully long, -1 = fully short).
    """

    params = {"fast": 20, "slow": 50, "rsi_len": 14}

    def setup(self):
        close = self.data["close"]
        self.fast = close.rolling(self.params["fast"]).mean()
        self.slow = close.rolling(self.params["slow"]).mean()
        self.rsi = self.indicator("rsi", length=self.params["rsi_len"]).iloc[:, 0]

    def next(self, i, bar):
        if pd.isna(self.slow.iloc[i]) or pd.isna(self.rsi.iloc[i]):
            return 0.0
        trending_up = self.fast.iloc[i] > self.slow.iloc[i]
        not_overbought = self.rsi.iloc[i] < 70
        return 1.0 if (trending_up and not_overbought) else 0.0
'''
