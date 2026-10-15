"""Tests for the calc skill (friday/skills/calc.py) — the safe AST evaluator.

Covers arithmetic/precedence, whitelisted functions + constants, the security
property (no eval, so code-injection expressions just fail to parse), the
exponent/factorial DoS guards, and the never-raise skill contract.
"""

from __future__ import annotations

import math

import pytest

from friday.skills.calc import calc, evaluate

# --- evaluate(): arithmetic + precedence ----------------------------------

@pytest.mark.parametrize("expr, want", [
    ("2+3", 5.0),
    ("15*27", 405.0),
    ("2*(3+4)", 14.0),          # parens override precedence
    ("2+3*4", 14.0),            # * binds tighter than +
    ("10/4", 2.5),             # true division
    ("10//4", 2.0),            # floor division
    ("10%3", 1.0),
    ("2**10", 1024.0),
    ("-5", -5.0),              # unary minus
    ("+5", 5.0),               # unary plus
    ("--5", 5.0),              # nested unary
])
def test_arithmetic(expr: str, want: float) -> None:
    assert evaluate(expr) == want


def test_functions_and_constants() -> None:
    assert evaluate("sqrt(2)") == pytest.approx(math.sqrt(2))
    assert evaluate("sin(pi/6)") == pytest.approx(0.5)
    assert evaluate("log(100, 10)") == pytest.approx(2.0)
    assert evaluate("max(1, 7, 3)") == 7.0
    assert evaluate("factorial(5)") == 120.0


# --- security: no eval, whitelist only ------------------------------------

@pytest.mark.parametrize("hostile", [
    "__import__('os').system('echo pwned')",
    "os.system('x')",
    "open('/etc/passwd')",
    "lambda: 1",
    "[x for x in range(3)]",
    "1 if True else 2",
    "True and False",          # bool ops aren't whitelisted
    "'a'+'b'",                 # string constants rejected
])
def test_hostile_expressions_rejected(hostile: str) -> None:
    with pytest.raises((ValueError, SyntaxError)):
        evaluate(hostile)


def test_unknown_name_and_function_rejected() -> None:
    with pytest.raises(ValueError):
        evaluate("foo")
    with pytest.raises(ValueError):
        evaluate("eval(1)")


# --- DoS guards: bounded exponent + factorial -----------------------------

def test_exponent_guard() -> None:
    with pytest.raises(ValueError, match="exponent too large"):
        evaluate("2**100000")


def test_factorial_guard() -> None:
    with pytest.raises(ValueError, match="factorial argument too large"):
        evaluate("factorial(999999)")


# --- skill contract: never raises, returns a dict -------------------------

async def test_calc_skill_success() -> None:
    res = await calc("15*27")
    assert res == {"expression": "15*27", "result": 405.0}


async def test_calc_skill_returns_error_not_raises() -> None:
    res = await calc("__import__('os')")
    assert "error" in res
    assert res["expression"] == "__import__('os')"


async def test_calc_skill_empty_expression() -> None:
    res = await calc("")
    assert "error" in res  # empty parse fails -> error dict, no exception
