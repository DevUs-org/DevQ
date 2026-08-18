'''
Tags: Main

Expression evaluator for OpenQASM 2.0 gate parameters.

The original reader dropped every parameter — `rx(pi/2)` lost its angle
entirely, so any parameterised gate (all of QASMBench) executed wrong.
This evaluates a parameter expression to a float against a binding of
named parameters, which is what custom-gate inlining substitutes into.

Grammar (standard precedence, ^ right-associative):

    expr   := term   (('+' | '-') term)*
    term   := unary  (('*' | '/') unary)*
    unary  := '-' unary | power
    power  := atom   ('^' unary)?
    atom   := NUMBER
            | 'pi'
            | FUNC '(' expr ')'
            | '(' expr ')'
            | IDENT                 -- a bound gate parameter

Supported: pi; + - * /; unary minus; ^ for power; and the unary
functions sin cos tan exp ln sqrt. 2.0's grammar also lists these, so
this is the language's own set, not an ad-hoc choice.

The evaluator works on the shared Token stream via a small cursor
object, so gate-argument parsing and expression parsing share one
position. It is deliberately a pure evaluator (expression -> float): it
never touches the circuit, so it can be tested in isolation.
'''

import math

from .tokenizer import QASMError


_FUNCS = {
    "sin":  math.sin,
    "cos":  math.cos,
    "tan":  math.tan,
    "exp":  math.exp,
    "ln":   math.log,     # 2.0's ln is natural log
    "sqrt": math.sqrt,
}


def evaluate(cursor, params):
    '''
    Parse and evaluate one parameter expression from `cursor`, consuming
    exactly the tokens of that expression.

    Args:
        cursor: a TokenCursor positioned at the first token of the
                expression. Advanced past the expression on return.
        params: {name: float} bindings for identifiers that appear as
                bare parameters (a custom gate's formal parameters).
                Empty for a top-level gate call, whose parameters must
                be constant expressions.

    Returns:
        float — the evaluated value.

    Raises:
        QASMError: on an unknown identifier, an unknown function, a
                   malformed expression, or a domain error (e.g. sqrt of
                   a negative, ln of zero).
    '''
    return _expr(cursor, params)


def _expr(cursor, params):
    value = _term(cursor, params)
    while cursor.peek().kind == "OP" and cursor.peek().value in "+-":
        op = cursor.next().value
        rhs = _term(cursor, params)
        value = value + rhs if op == "+" else value - rhs
    return value


def _term(cursor, params):
    value = _unary(cursor, params)
    while cursor.peek().kind == "OP" and cursor.peek().value in "*/":
        op = cursor.next().value
        rhs = _unary(cursor, params)
        if op == "*":
            value = value * rhs
        else:
            if rhs == 0:
                raise QASMError("division by zero in parameter expression",
                                cursor.peek().line)
            value = value / rhs
    return value


def _unary(cursor, params):
    if cursor.peek().kind == "OP" and cursor.peek().value == "-":
        cursor.next()
        return -_unary(cursor, params)
    return _power(cursor, params)


def _power(cursor, params):
    base = _atom(cursor, params)
    if cursor.peek().kind == "OP" and cursor.peek().value == "^":
        cursor.next()
        # Right-associative, and the exponent may itself be signed:
        # 2^3^2 == 2^(3^2), 2^-2 == 2^(-2).  Unary minus binds looser
        # than ^ as a base (so -2^2 == -(2^2)) but an explicit '-' after
        # '^' is the exponent's own sign, parsed via _unary here.
        exponent = _unary(cursor, params)
        try:
            return math.pow(base, exponent)
        except (ValueError, OverflowError) as e:
            raise QASMError(f"invalid power {base}^{exponent}: {e}",
                            cursor.peek().line)
    return base


def _atom(cursor, params):
    tok = cursor.peek()

    if tok.kind == "NUMBER":
        cursor.next()
        return float(tok.value)

    if tok.kind == "SYMBOL" and tok.value == "(":
        cursor.next()
        value = _expr(cursor, params)
        cursor.expect("SYMBOL", ")")
        return value

    if tok.kind == "ID":
        name = tok.value

        if name == "pi":
            cursor.next()
            return math.pi

        # A function application: name '(' expr ')'.
        if name in _FUNCS and cursor.peek(1).kind == "SYMBOL" \
                and cursor.peek(1).value == "(":
            cursor.next()          # function name
            cursor.next()          # '('
            arg = _expr(cursor, params)
            cursor.expect("SYMBOL", ")")
            try:
                return _FUNCS[name](arg)
            except (ValueError, OverflowError) as e:
                raise QASMError(f"{name}({arg}) is undefined: {e}", tok.line)

        # A bare identifier: a bound gate parameter.
        if name in params:
            cursor.next()
            return params[name]

        # An identifier that is neither pi, a function, nor a bound
        # parameter cannot appear in an expression. A top-level gate
        # call with a free variable lands here (its params must be
        # constant), as does a misspelled parameter name.
        raise QASMError(
            f"unknown name {name!r} in parameter expression "
            f"(expected pi, a function, or a gate parameter)", tok.line)

    raise QASMError(f"unexpected {tok.value!r} in parameter expression",
                    tok.line)