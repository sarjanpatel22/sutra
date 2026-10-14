"""Calculator skill — safe arithmetic, no eval().

Evaluates a math expression by walking a parsed AST and only allowing a
whitelist of operators, math functions, and constants. eval()/exec() are never
used, so a hostile expression ("__import__('os').system(...)") can't run — it
just fails to parse against the whitelist.
"""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Callable
from typing import Any

from friday.skills.registry import skill

_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau}
_FUNCS: dict[str, Callable[..., Any]] = {
    name: getattr(math, name)
    for name in (
        "sqrt", "cbrt", "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
        "log", "log2", "log10", "exp", "floor", "ceil", "factorial",
        "degrees", "radians", "hypot", "gcd", "fabs", "copysign", "fmod", "pow",
    )
    if hasattr(math, name)
}
_FUNCS.update({"abs": abs, "round": round, "min": min, "max": max, "sum": sum})

# Guards against expressions that are cheap to write but hang/OOM the event loop
# (this runs synchronously, so wait_for can't interrupt it once started).
_MAX_EXPONENT = 1000
_MAX_FACTORIAL = 10_000


def _eval(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError(f"unsupported constant: {node.value!r}")
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"unsupported operator: {type(node.op).__name__}")
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_EXPONENT:
            raise ValueError(f"exponent too large (>{_MAX_EXPONENT})")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        uop = _UNARY_OPS.get(type(node.op))
        if uop is None:
            raise ValueError(f"unsupported unary op: {type(node.op).__name__}")
        return uop(_eval(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            fn = getattr(node.func, "id", "?")
            raise ValueError(f"unsupported function: {fn}")
        args = [_eval(a) for a in node.args]
        if node.func.id == "factorial" and args and args[0] > _MAX_FACTORIAL:
            raise ValueError(f"factorial argument too large (>{_MAX_FACTORIAL})")
        return _FUNCS[node.func.id](*args)
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise ValueError(f"unknown name: {node.id}")
    raise ValueError(f"unsupported expression: {type(node).__name__}")


def evaluate(expression: str) -> float:
    """Safely evaluate a math expression string. Raises ValueError on anything
    outside the whitelist (operators, math functions, constants)."""
    tree = ast.parse(expression, mode="eval")
    return float(_eval(tree.body))


@skill(
    name="calc",
    description=(
        "THE tool for math and arithmetic — e.g. 'what is 15 times 27', '8 plus 5', "
        "percentages, sqrt, sin, log. Pass the expression (e.g. '15*27'). Not a shell command."
    ),
    auto_confirm=True,
    timeout_s=5,
)
async def calc(expression: str) -> dict[str, Any]:
    """Evaluate a mathematical expression.

    expression: Math to evaluate, e.g. "2*(3+4)", "sqrt(2)", "sin(pi/6)", "log(100, 10)".
    """
    try:
        result = evaluate(expression)
    except Exception as e:  # noqa: BLE001 — return the error to the model, don't raise
        return {"error": f"could not evaluate: {e}", "expression": expression}
    return {"expression": expression, "result": result}
