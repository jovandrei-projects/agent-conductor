"""Deterministic calculator - the read-only 'calc' tool (DESIGN 6.8).

A deliberately tiny arithmetic evaluator over `ast`: numbers, the four
operations plus // % **, parentheses, unary +/-, a fixed function set and
two constants. No names, no attribute access, no calls to anything but the
whitelisted functions, no strings - `eval` is never involved, so there is
nothing to escape into. Everything the assistant may compute lands here
(token/cost math, date deltas, ratios); anything else is an honest error.
"""
import ast
import math

MAX_EXPR_CHARS = 500
MAX_NODES = 120
MAX_ABS = 1e308          # reject overflow-sized literals early
MAX_POW_BITS = 65536     # ~8KB of digits - a model must never spin the
                        # server into a multi-GB bigint compute


class CalcError(ValueError):
    """A rejected expression - reported to the caller as a tool error."""


def _safe_pow(a, b):
    """a ** b with a bigint-magnitude guard: ``9**9**9`` is AST-valid and
    tiny in node count but produces an astronomically large int. Estimate
    the result's bit length before evaluating; floats can't explode in
    memory (they overflow to inf and _check rejects them)."""
    if isinstance(a, int) and isinstance(b, int) and b > 0 \
            and abs(a) > 1:
        est = b * max(1, a.bit_length())
        if est > MAX_POW_BITS:
            raise CalcError("power result would be too large")
    return a ** b


_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum,
    "floor": math.floor, "ceil": math.ceil, "trunc": math.trunc,
    "sqrt": math.sqrt, "log": math.log, "log2": math.log2,
    "log10": math.log10, "exp": math.exp, "pow": _safe_pow,
    "isnan": math.isnan, "isinf": math.isinf,
}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


_BINOPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
           ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
           ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
           ast.Pow: _safe_pow}
_UNOPS = {ast.UAdd: lambda a: +a, ast.USub: lambda a: -a}


def _check(val):
    if isinstance(val, (int, float)) and abs(val) > MAX_ABS:
        raise CalcError("number out of range")
    if isinstance(val, complex):
        raise CalcError("complex numbers are not supported")
    return val


def _eval(node) -> object:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or node.value is None:
            raise CalcError("only numbers are allowed")
        if not isinstance(node.value, (int, float)):
            raise CalcError("only numbers are allowed")
        return _check(node.value)
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise CalcError(f"operator {type(node.op).__name__} not allowed")
        try:
            return _check(op(_eval(node.left), _eval(node.right)))
        except (ArithmeticError, OverflowError) as exc:
            raise CalcError(str(exc)) from exc
    if isinstance(node, ast.UnaryOp):
        op = _UNOPS.get(type(node.op))
        if op is None:
            raise CalcError("unary operator not allowed")
        return _check(op(_eval(node.operand)))
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise CalcError(f"unknown name {node.id!r}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise CalcError("only whitelisted functions are allowed")
        if node.keywords:
            raise CalcError("keyword arguments are not supported")
        args = [_eval(a) for a in node.args]
        try:
            return _check(_FUNCS[node.func.id](*args))
        except (ArithmeticError, TypeError, ValueError,
                OverflowError) as exc:
            raise CalcError(f"{node.func.id}: {exc}") from exc
    if isinstance(node, (ast.List, ast.Tuple)):
        # a fixed-size literal sequence - exists so min/max/sum have input
        return [_eval(e) for e in node.elts]
    raise CalcError(f"{type(node).__name__} is not allowed")


def evaluate(expr: str) -> dict:
    """expr -> {"value": number|list, "expr": normalized} or raises
    CalcError. Deterministic and side-effect free."""
    expr = str(expr or "").strip()
    if not expr:
        raise CalcError("empty expression")
    if len(expr) > MAX_EXPR_CHARS:
        raise CalcError(f"expression over {MAX_EXPR_CHARS} characters")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise CalcError(f"invalid expression: {exc.msg}") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        raise CalcError("expression too complex")
    value = _eval(tree)
    if isinstance(value, float) and value != value:   # NaN
        raise CalcError("result is not a number")
    return {"value": value, "expr": expr}
