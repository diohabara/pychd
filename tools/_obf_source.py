"""Helper: apply an ``ObfuscationMapping`` to a Python source file.

The .pyc obfuscator (``pychd_pyobf``) operates on a *bytecode*-level
view: it renames everything that appears in ``co_names`` /
``co_varnames`` / ``co_consts`` / etc. To benchmark pychd against an
anonymised .pyc, we need an anonymised .py to compare the recovered
source *against* — otherwise every metric trips on the identifier
mismatch.

This module implements that transform at the AST level: parse the
original source, walk the tree, replace every identifier / docstring
/ string-literal occurrence according to the mapping, and unparse.
The result is a valid Python module that compiles to the same
opcode stream as the obfuscated .pyc.

Edge cases:

* Names not present in the mapping are left alone — they're either
  unused-by-bytecode (e.g. annotation strings under ``from __future__
  import annotations``) or things the compiler does not lift into a
  ``co_*`` slot (rare).
* Attribute accesses use the global names slot (`obj.attr` → `attr`
  appears in ``co_names``), so we rename them too.
* Imports are renamed in both their ``name`` and ``asname`` channels.
"""

from __future__ import annotations

import ast

from pychd_pyobf import ObfuscationMapping


def _all_identifier_renames(mapping: ObfuscationMapping) -> dict[str, str]:
    """Aggregate every identifier-style rename from the mapping.

    ``co_names`` (globals + attrs), ``co_varnames`` (locals),
    ``co_freevars`` / ``co_cellvars`` (closures), and the function-name
    mapping all share the same identifier namespace at source level.
    The string-constants mapping is *not* included here: it is
    handled separately for ``ast.Constant(value=str)`` nodes.
    """
    out: dict[str, str] = {}
    out.update(mapping.names)
    out.update(mapping.varnames)
    out.update(mapping.freevars)
    out.update(mapping.cellvars)
    out.update(mapping.co_names)
    return out


class _Renamer(ast.NodeTransformer):
    def __init__(self, ids: dict[str, str], consts: dict[str, str]) -> None:
        self._ids = ids
        self._consts = consts

    # ----- identifiers -----------------------------------------------------

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in self._ids:
            node.id = self._ids[node.id]
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if node.name in self._ids:
            node.name = self._ids[node.name]
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        if node.name in self._ids:
            node.name = self._ids[node.name]
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        if node.name in self._ids:
            node.name = self._ids[node.name]
        self.generic_visit(node)
        return node

    def visit_arg(self, node: ast.arg) -> ast.AST:
        if node.arg in self._ids:
            node.arg = self._ids[node.arg]
        self.generic_visit(node)
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        if node.attr in self._ids:
            node.attr = self._ids[node.attr]
        self.generic_visit(node)
        return node

    def visit_keyword(self, node: ast.keyword) -> ast.AST:
        if node.arg is not None and node.arg in self._ids:
            node.arg = self._ids[node.arg]
        self.generic_visit(node)
        return node

    def visit_Global(self, node: ast.Global) -> ast.AST:
        node.names = [self._ids.get(n, n) for n in node.names]
        return node

    def visit_Nonlocal(self, node: ast.Nonlocal) -> ast.AST:
        node.names = [self._ids.get(n, n) for n in node.names]
        return node

    def visit_alias(self, node: ast.alias) -> ast.AST:
        if node.name in self._ids:
            node.name = self._ids[node.name]
        if node.asname is not None and node.asname in self._ids:
            node.asname = self._ids[node.asname]
        return node

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST:
        # ImportFrom.module also lands in co_names.
        if node.module is not None and node.module in self._ids:
            node.module = self._ids[node.module]
        self.generic_visit(node)
        return node

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> ast.AST:
        if node.name is not None and node.name in self._ids:
            node.name = self._ids[node.name]
        self.generic_visit(node)
        return node

    # ----- match patterns --------------------------------------------------

    def visit_MatchAs(self, node: ast.MatchAs) -> ast.AST:
        if node.name is not None and node.name in self._ids:
            node.name = self._ids[node.name]
        self.generic_visit(node)
        return node

    def visit_MatchStar(self, node: ast.MatchStar) -> ast.AST:
        if node.name is not None and node.name in self._ids:
            node.name = self._ids[node.name]
        self.generic_visit(node)
        return node

    def visit_MatchMapping(self, node: ast.MatchMapping) -> ast.AST:
        if node.rest is not None and node.rest in self._ids:
            node.rest = self._ids[node.rest]
        self.generic_visit(node)
        return node

    # ----- constants -------------------------------------------------------

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, str) and node.value in self._consts:
            node.value = self._consts[node.value]
        return node


def apply_mapping_to_source(source: str, mapping: ObfuscationMapping) -> str:
    """Return *source* with every mapped identifier / docstring / string
    constant replaced according to *mapping*.

    The returned source compiles to the same opcode stream as the
    anonymised .pyc that *mapping* was extracted from, which makes
    it the right reference for ``signature_match`` / ``declaration_match``
    / ``strict_match`` when benchmarking pychd against the obfuscated
    .pyc.
    """
    tree = ast.parse(source)
    ids = _all_identifier_renames(mapping)
    new_tree = _Renamer(ids, mapping.consts).visit(tree)
    ast.fix_missing_locations(new_tree)
    return ast.unparse(new_tree)


__all__ = ["apply_mapping_to_source"]
