"""Scope tracking for the fuzzer.

The fuzzer must never emit a ``Load``-context ``Name`` that has not
been bound somewhere visible: Python is happy to compile a reference
to an unbound name (it errors only at runtime when the lookup hits
neither the local frame nor any enclosing one) but the recovered
source we benchmark against expects definitions to be visible. So we
maintain an explicit lexical-scope stack while we build the AST and
hand the builders a guaranteed-defined name pool.

A ``Scope`` carries:

* ``locals``    — names bound in this scope
* ``params``    — function parameters / class type parameters (separate
                  bucket so the fuzzer can prefer them when picking a
                  free variable for an inner closure)
* ``parent``    — enclosing scope (None for the module scope)
* ``kind``      — ``"module"`` / ``"function"`` / ``"class"`` /
                  ``"comprehension"`` (decides shadowing rules)

``visible_names()`` walks the chain and returns every binding the
``Name`` builder is allowed to reference. We intentionally do not
model walrus or class-level scoping rules to the letter — the fuzzer
only needs enough fidelity that ``compile()`` accepts the output,
which it does.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Builtins that the fuzzer is allowed to call/reference without
# defining first. Kept small and stable so we do not accidentally
# emit something that landed in `dir(builtins)` only in 3.12+.
SAFE_BUILTINS = (
    "abs",
    "all",
    "any",
    "bool",
    "bytes",
    "callable",
    "dict",
    "enumerate",
    "filter",
    "float",
    "getattr",
    "hasattr",
    "id",
    "int",
    "isinstance",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "object",
    "print",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
    "type",
    "zip",
)


@dataclass
class Scope:
    """A single lexical scope in the fuzzer's scope chain."""

    kind: str  # "module" | "function" | "class" | "comprehension"
    parent: Scope | None = None
    locals: set[str] = field(default_factory=set)
    params: set[str] = field(default_factory=set)
    # True when this scope (or an enclosing function-shaped scope)
    # was opened by an ``async def`` — the fuzzer uses this to gate
    # ``async for`` / ``async with`` / ``await`` emission.
    is_async: bool = False

    def in_async_function(self) -> bool:
        """Are we lexically inside an ``async def`` (ignoring class /
        sync-function boundaries)?"""
        s: Scope | None = self
        while s is not None:
            if s.kind == "function":
                # The closest enclosing function determines async-ness;
                # async-ness does not leak out of a nested sync def.
                return s.is_async
            s = s.parent
        return False

    def bind(self, name: str) -> None:
        """Mark ``name`` as defined in this scope."""
        self.locals.add(name)

    def bind_param(self, name: str) -> None:
        self.params.add(name)

    def visible_names(self) -> list[str]:
        """Names that a ``Load``-context Name node may reference."""
        names: set[str] = set(SAFE_BUILTINS)
        s: Scope | None = self
        while s is not None:
            # Class bodies do NOT contribute to inner scopes — their
            # bindings live in the class namespace, not in enclosing
            # closures — but we keep them visible for `self.attr`
            # style lookups inside the class body itself.
            if s is self or s.kind != "class":
                names.update(s.locals)
                names.update(s.params)
            s = s.parent
        return sorted(names)

    def child(self, kind: str, *, is_async: bool = False) -> Scope:
        """Open a nested scope of *kind*.

        ``is_async`` defaults to False; set it explicitly when
        opening a scope for an ``async def``. A class / comprehension
        scope inherits its enclosing function's async-ness via
        :meth:`in_async_function` so the caller usually does not need
        to pass it.
        """
        return Scope(kind=kind, parent=self, is_async=is_async)


def module_scope() -> Scope:
    """Top-level scope factory."""
    return Scope(kind="module")


__all__ = ["SAFE_BUILTINS", "Scope", "module_scope"]
