"""Tagged-union arithmetic expression evaluated via PEP 634 match."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Lit:
    value: float


@dataclass(frozen=True)
class Var:
    name: str


@dataclass(frozen=True)
class Add:
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class Mul:
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class Let:
    name: str
    bound: "Expr"
    body: "Expr"


Expr = Lit | Var | Add | Mul | Let


def evaluate(expr: Expr, env: dict[str, float] | None = None) -> float:
    env = env or {}
    match expr:
        case Lit(value=v):
            return v
        case Var(name=n):
            if n not in env:
                raise KeyError(f"unbound symbol: {n}")
            return env[n]
        case Add(left=lhs, right=rhs):
            return evaluate(lhs, env) + evaluate(rhs, env)
        case Mul(left=lhs, right=rhs):
            return evaluate(lhs, env) * evaluate(rhs, env)
        case Let(name=n, bound=b, body=body):
            updated = {**env, n: evaluate(b, env)}
            return evaluate(body, updated)


def free_vars(expr: Expr) -> set[str]:
    match expr:
        case Lit():
            return set()
        case Var(name=n):
            return {n}
        case Add(left=lhs, right=rhs) | Mul(left=lhs, right=rhs):
            return free_vars(lhs) | free_vars(rhs)
        case Let(name=n, bound=b, body=body):
            return (free_vars(body) - {n}) | free_vars(b)
        case _:
            return set()


def example_program() -> Expr:
    return Let(
        "k",
        Add(Lit(2.0), Lit(3.0)),
        Mul(Var("k"), Add(Var("k"), Lit(1.0))),
    )
