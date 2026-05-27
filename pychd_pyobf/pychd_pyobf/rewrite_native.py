"""Native (3.14 / running-interpreter) .pyc anonymiser.

Uses :func:`marshal.loads` + :meth:`types.CodeType.replace` to rewrite
identifiers, constants, and metadata recursively, then re-marshals the
top-level code object. The opcode stream (``co_code``) is preserved
byte-for-byte so :mod:`dis` still walks the result and pychd's rule
pass still sees the same instruction structure.

The cross-version path (``rewrite_subprocess``) reuses the same
algorithm, just executed inside a subprocess running the target
interpreter.

Anonymisation rules (kept in sync with the package docstring):

* ``co_names``     → ``_n0, _n1, …``
* ``co_varnames``  → ``_v0, _v1, …``
* ``co_freevars``  → ``_f0, _f1, …``
* ``co_cellvars``  → ``_c0, _c1, …``
* ``co_consts``    → string literals → ``_s0, _s1, …``; other
                     primitives left alone; tuples / frozensets
                     mapped recursively; nested code objects
                     recursively anonymised
* ``co_name``      → per-depth ``_fn0, _fn1, …``
* ``co_qualname``  → same per-depth scheme (3.11+ only)
* ``co_filename``  → fixed literal ``"<anonymised>"``
* ``co_lnotab`` /
  ``co_linetable`` /
  ``co_positions``→ replaced with empty bytes — pychd's rule pass
                    does not depend on line info
* ``co_firstlineno`` → 1
* docstring (the leading ``co_consts[0]`` when it is a ``str``) →
  retained as a string but rewritten via the same ``co_consts``
  mapping (so it ends up as ``_sN`` rather than its original text)

The function returns an :class:`ObfuscationMapping` so callers can
audit the rewriting (and so the unit tests can assert that every
emitted identifier starts with the expected prefix).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import CodeType


@dataclass
class ObfuscationMapping:
    """Original → anonymised name table, returned alongside the rewrite."""

    names: dict[str, str] = field(default_factory=dict)
    varnames: dict[str, str] = field(default_factory=dict)
    freevars: dict[str, str] = field(default_factory=dict)
    cellvars: dict[str, str] = field(default_factory=dict)
    consts: dict[str, str] = field(default_factory=dict)
    co_names: dict[str, str] = field(default_factory=dict)  # co_name (function name)

    def to_dict(self) -> dict[str, dict[str, str]]:
        return {
            "names": dict(self.names),
            "varnames": dict(self.varnames),
            "freevars": dict(self.freevars),
            "cellvars": dict(self.cellvars),
            "consts": dict(self.consts),
            "co_names": dict(self.co_names),
        }


_ANON_FILENAME = "<anonymised>"


def _anonymise_tuple(
    original: tuple[str, ...],
    prefix: str,
    mapping: dict[str, str],
) -> tuple[str, ...]:
    """Rewrite *original* (a tuple of strings) into ``_<prefix>N`` form,
    growing *mapping* with the rename pairs."""
    out: list[str] = []
    for i, name in enumerate(original):
        if name in mapping:
            out.append(mapping[name])
            continue
        new_name = f"_{prefix}{i}"
        mapping[name] = new_name
        out.append(new_name)
    return tuple(out)


def _anonymise_const(
    const: object,
    mapping: ObfuscationMapping,
    depth: int,
    depth_counter: dict[int, int],
) -> object:
    """Recursively rewrite a ``co_consts`` entry.

    * Strings become ``_sN`` (interned across the whole code-object
      tree so equal strings get the same anonymised name).
    * Tuples / frozensets are remapped element-by-element so they
      remain hashable.
    * Nested :class:`CodeType` objects are recursively anonymised.
    * Numbers, bytes, ``None``, ``True``, ``False``, ``Ellipsis`` are
      preserved (the LLM cannot infer source identity from a numeric
      literal that the rule pass also sees verbatim).
    """
    if isinstance(const, str):
        if const in mapping.consts:
            return mapping.consts[const]
        new = f"_s{len(mapping.consts)}"
        mapping.consts[const] = new
        return new
    if isinstance(const, tuple):
        return tuple(
            _anonymise_const(item, mapping, depth, depth_counter) for item in const
        )
    if isinstance(const, frozenset):
        return frozenset(
            _anonymise_const(item, mapping, depth, depth_counter) for item in const
        )
    if isinstance(const, CodeType):
        return _anonymise_code(const, mapping, depth + 1, depth_counter)
    # int / float / complex / bool / None / bytes / Ellipsis: keep.
    return const


def _empty_lineinfo() -> bytes:
    return b""


def _anonymise_code(
    code: CodeType,
    mapping: ObfuscationMapping,
    depth: int,
    depth_counter: dict[int, int],
) -> CodeType:
    """Return a new :class:`CodeType` with anonymised identifiers."""
    # Identifier tuples.
    new_names = _anonymise_tuple(code.co_names, "n", mapping.names)
    new_varnames = _anonymise_tuple(code.co_varnames, "v", mapping.varnames)
    new_freevars = _anonymise_tuple(code.co_freevars, "f", mapping.freevars)
    new_cellvars = _anonymise_tuple(code.co_cellvars, "c", mapping.cellvars)

    # Constants (recursive).
    new_consts = tuple(
        _anonymise_const(c, mapping, depth, depth_counter) for c in code.co_consts
    )

    # Per-depth function name counter — ``_fn0`` at depth 0,
    # ``_fn1, _fn2, …`` for nested defs.
    n_at_depth = depth_counter.setdefault(depth, 0)
    new_co_name = f"_fn{depth}_{n_at_depth}"
    depth_counter[depth] = n_at_depth + 1
    mapping.co_names[code.co_name] = new_co_name

    # First do the always-supported rewrite. The remaining kwargs are
    # version-conditional and applied via a second ``replace`` call so
    # we keep the strict signature of the first call for the type
    # checker while still letting older interpreters skip kwargs they
    # do not accept.
    new_code = code.replace(
        co_names=new_names,
        co_varnames=new_varnames,
        co_freevars=new_freevars,
        co_cellvars=new_cellvars,
        co_consts=new_consts,
        co_name=new_co_name,
        co_filename=_ANON_FILENAME,
        co_firstlineno=1,
    )
    # Optional fields. Each ``replace`` returns a fresh CodeType, so
    # chaining is fine.
    if hasattr(new_code, "co_qualname"):
        new_code = new_code.replace(co_qualname=new_co_name)
    if hasattr(new_code, "co_linetable"):
        # 3.11+ uses ``co_linetable`` as the canonical line table.
        new_code = new_code.replace(co_linetable=_empty_lineinfo())
    # On 3.10 and earlier, ``co_lnotab`` is the canonical line table.
    # We suppress the deprecation warning that ``hasattr(code,
    # "co_lnotab")`` raises on 3.11+ where the attribute is now a
    # read-only alias and ``replace()`` no longer accepts the kwarg.
    import sys as _sys
    import warnings as _warnings

    if _sys.version_info < (3, 11):
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore", DeprecationWarning)
            if hasattr(new_code, "co_lnotab"):
                new_code = new_code.replace(co_lnotab=_empty_lineinfo())
    # ``co_exceptiontable`` (3.11+) carries try/except metadata; the
    # opcode stream still needs valid handler offsets so we leave it
    # alone. ``co_positions`` is computed lazily from co_linetable so
    # zeroing the table is enough.
    return new_code


def anonymise(code: CodeType) -> tuple[CodeType, ObfuscationMapping]:
    """Public entry point: anonymise a top-level code object."""
    mapping = ObfuscationMapping()
    new_code = _anonymise_code(code, mapping, depth=0, depth_counter={})
    return new_code, mapping


__all__ = ["ObfuscationMapping", "anonymise"]
