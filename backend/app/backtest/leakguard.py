"""LeakGuard — static detection of well-known lookahead-bias footguns in
BYOC strategy code, on top of sandbox.py's security allowlist.

sandbox.py's AST allowlist stops malicious/unsafe code; it says nothing
about *correct-looking* code that is nonetheless biased. The engine's own
`target.shift(1)` (see backtest/engine.py's no-lookahead guarantee) is a
blanket safety net applied to a strategy's *output* signal after it
returns — it cannot see inside a strategy's own `setup()`/`next()` methods.
A precomputed series built in `setup()` with `.shift(-N)`,
`.rolling(..., center=True)`, or `.fillna(method="bfill")` has already read
future bars by the time `next(i, bar)` reads it via `self.fast.iloc[i]`;
the engine's output shift, applied later to whatever `next()` returns,
never touches that internal computation. Likewise `next(i, bar)` directly
indexing `self.data[...].iloc[i + 1]` reads a future row outright.

This is deliberately a **finite set of well-known, provable patterns**
caught by direct AST pattern-matching — real, common quant/pandas footguns
(a centered rolling window and `bfill` are two of the most frequently
misused pandas calls in exactly this way) — not a general abstract
interpreter over arbitrary pandas code. An empty result means none of these
specific patterns were found, not a proof the strategy is bias-free: an
`.apply()` with an arbitrary lambda, dynamic column selection, or any
pattern not listed here passes through unflagged. Widening this into a
temporal-offset abstract interpreter (tracking a whole expression's
[min, max] bar-offset through arbitrary chains of operations, with an
LLM-proposed-and-mechanically-verified escalation path for whatever it
can't decide statically) is a natural next iteration, not attempted here.
"""
from __future__ import annotations

import ast


class LookaheadFinding:
    __slots__ = ("line", "message")

    def __init__(self, line: int, message: str):
        self.line = line
        self.message = message

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}"


def _is_positive_number_literal(node: ast.AST) -> int | float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and node.value > 0:
        return node.value
    return None


def _is_negative_shift_arg(node: ast.AST) -> bool:
    """True for `-5`, `-1.0`, etc: `ast.UnaryOp(USub, Constant(positive))`,
    or (rare, but valid AST) a directly negative `Constant`."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return _is_positive_number_literal(node.operand) is not None
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value < 0
    return False


def _keyword_value(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _forward_index_offset(node: ast.AST, index_name: str) -> int | float | None:
    """For `<index_name> + k` (k > 0) returns k; for `<index_name> - k`
    returns -k (a backward, safe offset); else None if this subscript's
    slice isn't a recognized `<name> +/- <constant>` shape at all."""
    if isinstance(node, ast.BinOp) and isinstance(node.left, ast.Name) and node.left.id == index_name:
        if isinstance(node.op, ast.Add):
            k = _is_positive_number_literal(node.right)
            if k is not None:
                return k
        elif isinstance(node.op, ast.Sub):
            k = _is_positive_number_literal(node.right)
            if k is not None:
                return -k
    return None


class _LookaheadVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.findings: list[LookaheadFinding] = []
        # Stack of the per-bar loop-index parameter name (e.g. "i") while
        # walking inside a next()/on_bar() method body — forward indexing
        # only means something relative to "the current bar" inside those
        # two hooks; the same `i` used as an ordinary local elsewhere (e.g.
        # a comprehension in setup()) isn't this pattern at all.
        self._index_names: list[str] = []

    def _flag(self, node: ast.AST, message: str) -> None:
        self.findings.append(LookaheadFinding(getattr(node, "lineno", 0), message))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        is_per_bar_hook = node.name in ("next", "on_bar") and len(node.args.args) >= 2
        index_name = node.args.args[1].arg if is_per_bar_hook else None
        if index_name:
            self._index_names.append(index_name)
        self.generic_visit(node)
        if index_name:
            self._index_names.pop()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        attr = func.attr if isinstance(func, ast.Attribute) else None

        if attr == "shift" and node.args and _is_negative_shift_arg(node.args[0]):
            self._flag(
                node,
                "shift() with a negative argument reads future bars — "
                "use a positive shift (backward-looking) or remove the call",
            )

        elif attr == "rolling":
            center = _keyword_value(node, "center")
            if center is not None and not (isinstance(center, ast.Constant) and center.value is False):
                self._flag(
                    node,
                    "rolling(..., center=True) averages in future bars — "
                    "centered windows are not causal; omit center or set it False",
                )

        elif attr == "bfill":
            self._flag(
                node,
                "bfill() fills a gap using the NEXT valid observation — a future "
                "value — use ffill() (or fillna(0)) for a causal fill",
            )

        elif attr == "fillna":
            method = _keyword_value(node, "method")
            if isinstance(method, ast.Constant) and method.value in ("bfill", "backfill"):
                self._flag(
                    node,
                    f"fillna(method={method.value!r}) fills a gap using the NEXT valid "
                    "observation — a future value — use 'ffill' for a causal fill",
                )

        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802
        if self._index_names:
            current = self._index_names[-1]
            offset = _forward_index_offset(node.slice, current)
            if offset is not None and offset > 0:
                self._flag(
                    node,
                    f"indexing {offset} bar(s) ahead of the current bar "
                    f"('{current} + {offset}') reads future data",
                )
        self.generic_visit(node)


def detect_lookahead(source: str) -> list[str]:
    """Static lookahead-bias findings, formatted as "line N: message"
    strings — the exact same shape as sandbox.validate_code()'s return, so
    the two lists merge into one with no caller-side changes. Returns []
    when none of the known patterns are found; see the module docstring for
    exactly what that does and doesn't prove. A source string that fails to
    parse returns [] here too — sandbox.validate_code() already reports the
    SyntaxError itself, and re-reporting it from a second AST pass would
    just be noise.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    visitor = _LookaheadVisitor()
    visitor.visit(tree)
    return [str(finding) for finding in visitor.findings]
