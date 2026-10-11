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

Layers (defense-in-depth; this is NOT a hostile-multitenant jail — the child
still shares the web container's user and network stack):
  1. Feature flag: settings.CUSTOM_STRATEGIES_ENABLED=false turns BYOC off
     entirely (registry hides it, validate/submit return 403, runner refuses).
  2. AST allowlist, checked in the web process before anything executes: no
     imports, no dunder/underscore access, no exec/eval/open, no
     global/nonlocal, and no attribute whose name is a pandas/numpy file or
     network entry point (read_*, to_csv, np.load, tofile, ...).
  3. User code NEVER executes in the web process. `check_custom_strategy` and
     `run_custom_strategy_isolated` start a fresh Python child
     (app.backtest.sandbox_worker) with an empty environment (no DATABASE_URL,
     no JWT_SECRET, nothing inherited), a throwaway working directory, and a
     hard wall-clock timeout after which the whole process group is SIGKILLed.
  4. Inside the child, before user code runs: RLIMIT_CPU, RLIMIT_AS (memory;
     enforced on Linux, ignored by macOS), RLIMIT_FSIZE=0, RLIMIT_NPROC=0,
     RLIMIT_CORE=0, plus a PEP 578 audit hook that rejects sockets, process
     creation, file writes, and file reads outside the Python installation.
  5. Code executes under curated builtins with *restricted* pd/np namespaces:
     the I/O functions are simply not present.
  6. The child returns plain JSON (never pickle), which the parent validates,
     re-aligns to the data index and clips to [-1, 1].
"""
from __future__ import annotations

import ast
import builtins as _builtins
import json
import logging
import math
import os
import pickle
import signal
import subprocess
import sys
import tempfile
import types
import warnings

import numpy as np
import pandas as pd

from app.backtest.base import BaseStrategy
from app.indicators import IndicatorService, IndicatorSpec

logger = logging.getLogger("backtest.sandbox")

MAX_CODE_BYTES = 64 * 1024
#: cap on error text relayed from the child (user-controlled exception messages)
MAX_ERROR_CHARS = 2000

_FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "open", "input", "breakpoint", "__import__",
    "getattr", "setattr", "delattr", "vars", "globals", "locals", "memoryview",
    "exit", "quit", "help",
}

#: pandas/numpy members that touch the filesystem/network or evaluate strings.
#: Rejected as attribute names by the AST gate AND left out of the pd/np
#: namespaces handed to user code. `read_*` is matched as a prefix.
_IO_ATTRS = frozenset({
    # pandas writers
    "to_csv", "to_excel", "to_json", "to_html", "to_latex", "to_markdown",
    "to_parquet", "to_feather", "to_orc", "to_pickle", "to_hdf", "to_sql",
    "to_stata", "to_gbq", "to_clipboard", "to_xml", "to_string",
    # numpy file I/O + raw-memory escape hatches
    "load", "loads", "save", "savez", "savez_compressed", "savetxt", "loadtxt",
    "genfromtxt", "fromfile", "fromregex", "tofile", "dump", "dumps", "memmap",
    "DataSource", "ctypes", "ctypeslib", "lib", "f2py", "distutils", "testing",
    # pandas I/O namespaces, plotting backends, string evaluation, introspection
    "io", "api", "plotting", "plot", "style", "ExcelWriter", "ExcelFile",
    "HDFStore", "eval", "query", "show_versions", "info", "source",
})


def _is_io_name(name: str) -> bool:
    return name in _IO_ATTRS or name.startswith("read_")


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


def _restricted_module(mod: types.ModuleType, submodules: tuple[str, ...] = ()
                       ) -> types.SimpleNamespace:
    """A copy of `mod`'s public surface minus I/O members and submodules
    (except the explicitly allowed, themselves restricted, `submodules`)."""
    ns = types.SimpleNamespace()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for name in dir(mod):
            if name.startswith("_") or _is_io_name(name):
                continue
            try:
                value = getattr(mod, name)
            except Exception:  # noqa: BLE001 — deprecated/lazy attrs that raise
                continue
            if isinstance(value, types.ModuleType):
                if name not in submodules:
                    continue
                value = _restricted_module(value)
            setattr(ns, name, value)
    return ns


#: what user code sees as `pd` / `np` — no read_*/to_*/load/save/fromfile/...
SAFE_PD = _restricted_module(pd, submodules=("offsets",))
SAFE_NP = _restricted_module(np, submodules=("linalg", "random", "fft"))


class SandboxError(ValueError):
    """User code rejected or crashed — surfaced verbatim to the submitter."""


class SandboxDisabledError(SandboxError):
    """Custom strategies are switched off on this deployment."""


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
        elif _is_io_name(node.attr):
            self._deny(node, f"'{node.attr}' is not available in the sandbox "
                             f"(no file or network I/O)")
        self.generic_visit(node)

    def visit_Name(self, node):  # noqa: N802
        if node.id.startswith("__") or node.id in _FORBIDDEN_CALLS:
            self._deny(node, f"'{node.id}' is not allowed in the sandbox")
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):  # noqa: N802
        self._deny(node, "async code is not allowed")


def validate_code(source: str) -> list[str]:
    """Static errors in user code; empty list means it passed the gate.
    Pure AST inspection — safe to call in the web process."""
    if not isinstance(source, str):
        return ["code must be a string"]
    if len(source.encode()) > MAX_CODE_BYTES:
        return [f"source exceeds {MAX_CODE_BYTES // 1024}KB"]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"line {exc.lineno}: {exc.msg}"]
    v = _Validator()
    v.visit(tree)
    return v.errors


# --------------------------------------------- in-process primitives -------
# These EXECUTE user code in the calling process. The web app must never call
# them directly — they run inside app.backtest.sandbox_worker (and unit tests).
def build_custom_strategy(source: str, params: dict | None = None) -> CustomStrategy:
    """Validate, exec under restricted globals, and instantiate the single
    CustomStrategy subclass the user defined. Runs user code in THIS process."""
    errors = validate_code(source)
    if errors:
        raise SandboxError("; ".join(errors))

    namespace: dict = {
        "__builtins__": _SAFE_BUILTINS,
        "__name__": "user_strategy",   # class creation reads the module name
        "CustomStrategy": CustomStrategy,
        "pd": SAFE_PD, "np": SAFE_NP, "math": math,
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
    """Execute + validate output: a numeric Series aligned to data, in [-1, 1].
    Runs user code in THIS process."""
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


# ------------------------------------------------- isolated execution ------
#: backend/ — the child puts this on sys.path to import the `app` package
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_WORKER_BOOTSTRAP = ("import sys; sys.path.insert(0, sys.argv[1]); "
                     "from app.backtest.sandbox_worker import main; main()")


def custom_strategies_enabled() -> bool:
    from app.core.config import settings
    return bool(settings.CUSTOM_STRATEGIES_ENABLED)


def _require_enabled() -> None:
    if not custom_strategies_enabled():
        raise SandboxDisabledError("custom strategies are disabled on this deployment")


def child_env(home: str) -> dict[str, str]:
    """The child's ENTIRE environment. Nothing is inherited from the web
    process, so DATABASE_URL / JWT_SECRET / POSTGRES_* never reach user code."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": home,
        "TMPDIR": home,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        # single-threaded numeric libs: no thread pools to account for under
        # RLIMIT_NPROC / RLIMIT_AS, and predictable CPU accounting
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_MAX_THREADS": "1",
        "NUMBA_NUM_THREADS": "1",
    }


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _run_worker(request: dict, timeout_s: float) -> dict:
    """Run one request in a fresh sandbox child; return its parsed JSON reply.
    Raises SandboxError on timeout, crash, or a malformed/negative reply."""
    from app.core.config import settings

    request = {**request, "limits": {
        "cpu_s": int(math.ceil(timeout_s)) + 1,
        "memory_mb": int(settings.CUSTOM_STRATEGY_MEMORY_MB),
    }}
    payload = pickle.dumps(request, protocol=pickle.HIGHEST_PROTOCOL)
    with tempfile.TemporaryDirectory(prefix="byoc-") as home:
        try:
            proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
                [sys.executable, "-I", "-B", "-c", _WORKER_BOOTSTRAP, _BACKEND_ROOT],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=home, env=child_env(home), close_fds=True,
                start_new_session=True,   # own process group -> killpg on timeout
            )
        except OSError as exc:
            raise SandboxError(f"could not start the strategy sandbox: {exc}") from exc
        try:
            out, err = proc.communicate(payload, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.communicate()
            raise SandboxError(
                f"strategy exceeded the {timeout_s:g}s time limit and was stopped") from None
        except BaseException:
            _kill_group(proc)
            proc.communicate()
            raise

    reply = None
    lines = out.strip().splitlines()
    if lines:
        try:
            reply = json.loads(lines[-1])
        except ValueError:
            reply = None
    if not isinstance(reply, dict):
        rc = proc.returncode
        if rc in (-signal.SIGXCPU, -signal.SIGKILL):
            raise SandboxError("strategy exceeded its CPU time limit and was stopped")
        logger.warning("sandbox worker exited rc=%s without a reply; stderr tail: %s",
                       rc, err[-MAX_ERROR_CHARS:].decode(errors="replace"))
        raise SandboxError(f"strategy sandbox exited unexpectedly (exit code {rc})")
    if not reply.get("ok"):
        raise SandboxError(str(reply.get("error") or "unknown sandbox error")[:MAX_ERROR_CHARS])
    return reply


def check_custom_strategy(source: str, params: dict | None = None) -> str:
    """Static gate, then define + instantiate the class in a sandbox child
    (class bodies are code too). Returns the user's class name."""
    from app.core.config import settings

    _require_enabled()
    errors = validate_code(source)
    if errors:
        raise SandboxError("; ".join(errors))
    reply = _run_worker({"op": "check", "source": source, "params": dict(params or {})},
                        float(settings.CUSTOM_STRATEGY_VALIDATE_TIMEOUT_S))
    return str(reply.get("class_name", ""))


def run_custom_strategy_isolated(source: str, params: dict | None,
                                 data: pd.DataFrame) -> tuple[pd.Series, str]:
    """Build + run the user's strategy on `data` in a sandbox child.
    Returns (weights aligned to data.index and clipped to [-1, 1], class name)."""
    from app.core.config import settings

    _require_enabled()
    errors = validate_code(source)
    if errors:
        raise SandboxError("; ".join(errors))
    reply = _run_worker({"op": "run", "source": source, "params": dict(params or {}),
                         "data": data},
                        float(settings.CUSTOM_STRATEGY_TIMEOUT_S))
    weights = reply.get("weights")
    if (not isinstance(weights, list) or len(weights) != len(data)
            or not all(isinstance(w, (int, float)) and not isinstance(w, bool)
                       for w in weights)):
        raise SandboxError("strategy sandbox returned a malformed result")
    out = pd.Series(np.asarray(weights, dtype=float), index=data.index)
    return out.fillna(0.0).clip(-1.0, 1.0), str(reply.get("class_name", ""))


def probe_sandbox() -> dict:
    """Diagnostics from inside a sandbox child (env keys, rlimits, which
    guarded operations are blocked). Never runs user code; used by tests."""
    from app.core.config import settings
    return _run_worker({"op": "probe"}, float(settings.CUSTOM_STRATEGY_VALIDATE_TIMEOUT_S))


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
