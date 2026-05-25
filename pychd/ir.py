"""Intermediate representation for partially-recovered Python source.

The rule-based decompiler emits IR nodes. Each node knows how to render
itself back to Python source via `render(indent)`. Nodes whose contents
the rule engine could not recover are represented as `UnknownBlock`,
which can later be filled in by the LLM pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_INDENT = "    "


@dataclass
class Arg:
    name: str
    annotation: str | None = None
    default: str | None = None


@dataclass
class Arguments:
    posonly: list[Arg] = field(default_factory=list)
    args: list[Arg] = field(default_factory=list)
    vararg: Arg | None = None
    kwonly: list[Arg] = field(default_factory=list)
    kwarg: Arg | None = None

    def render(self) -> str:
        parts: list[str] = []
        for a in self.posonly:
            parts.append(_render_arg(a))
        if self.posonly:
            parts.append("/")
        for a in self.args:
            parts.append(_render_arg(a))
        if self.vararg is not None:
            parts.append("*" + _render_arg(self.vararg))
        elif self.kwonly:
            parts.append("*")
        for a in self.kwonly:
            parts.append(_render_arg(a))
        if self.kwarg is not None:
            parts.append("**" + _render_arg(self.kwarg))
        return ", ".join(parts)


def _render_arg(a: Arg) -> str:
    out = a.name
    if a.annotation is not None:
        out += f": {a.annotation}"
    if a.default is not None:
        out += f" = {a.default}" if a.annotation else f"={a.default}"
    return out


class Stmt:
    """Marker base; every concrete IR statement has a render(indent)."""

    def render(self, indent: int = 0) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass
class Docstring(Stmt):
    text: str

    def render(self, indent: int = 0) -> str:
        import warnings

        pad = _INDENT * indent
        text = self.text
        # Triple-quoted output can break in several non-obvious ways:
        # embedded ``"""``, trailing ``"`` or ``\``, null bytes, or
        # un-decodeable escape sequences. We attempt the natural
        # rendering first and verify it compiles; on failure, fall back
        # to ``repr()`` which is always a valid Python string literal.
        candidate = f'"""{text}"""'
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                compile(candidate, "<doc>", "exec")
        except SyntaxError, ValueError:
            return f"{pad}{text!r}"
        return f"{pad}{candidate}"


@dataclass
class Import(Stmt):
    names: list[tuple[str, str | None]]  # (dotted, asname)

    def render(self, indent: int = 0) -> str:
        pad = _INDENT * indent
        rendered: list[str] = []
        for name, asname in self.names:
            rendered.append(f"{name} as {asname}" if asname else name)
        return f"{pad}import {', '.join(rendered)}"


@dataclass
class FromImport(Stmt):
    module: str
    level: int  # for `from .x import y`
    names: list[tuple[str, str | None]]  # (name, asname) or [('*', None)]

    def render(self, indent: int = 0) -> str:
        pad = _INDENT * indent
        prefix = "." * self.level + self.module
        if self.names == [("*", None)]:
            return f"{pad}from {prefix} import *"
        rendered: list[str] = []
        for name, asname in self.names:
            rendered.append(f"{name} as {asname}" if asname else name)
        return f"{pad}from {prefix} import {', '.join(rendered)}"


@dataclass
class Assign(Stmt):
    target: str
    value: str  # already rendered as Python source
    annotation: str | None = None

    def render(self, indent: int = 0) -> str:
        pad = _INDENT * indent
        if self.annotation is not None:
            return f"{pad}{self.target}: {self.annotation} = {self.value}"
        return f"{pad}{self.target} = {self.value}"


@dataclass
class AnnotationOnly(Stmt):
    target: str
    annotation: str

    def render(self, indent: int = 0) -> str:
        pad = _INDENT * indent
        return f"{pad}{self.target}: {self.annotation}"


@dataclass
class FunctionDef(Stmt):
    name: str
    args: Arguments
    decorators: list[str] = field(default_factory=list)
    is_async: bool = False
    is_generator: bool = False
    return_annotation: str | None = None
    docstring: str | None = None
    body: list[Stmt] = field(default_factory=list)

    def render(self, indent: int = 0) -> str:
        pad = _INDENT * indent
        lines: list[str] = []
        for deco in self.decorators:
            lines.append(f"{pad}@{deco}")
        kw = "async def" if self.is_async else "def"
        ret = f" -> {self.return_annotation}" if self.return_annotation else ""
        lines.append(f"{pad}{kw} {self.name}({self.args.render()}){ret}:")
        body_lines: list[str] = []
        if self.docstring is not None:
            body_lines.append(Docstring(self.docstring).render(indent + 1))
        if not self.body and self.docstring is None:
            body_lines.append(_INDENT * (indent + 1) + "pass")
        else:
            for stmt in self.body:
                body_lines.append(stmt.render(indent + 1))
        return "\n".join(lines + body_lines)


@dataclass
class ClassDef(Stmt):
    name: str
    bases: list[str] = field(default_factory=list)
    keywords: list[tuple[str, str]] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    docstring: str | None = None
    body: list[Stmt] = field(default_factory=list)

    def render(self, indent: int = 0) -> str:
        pad = _INDENT * indent
        lines: list[str] = []
        for deco in self.decorators:
            lines.append(f"{pad}@{deco}")
        header_args: list[str] = list(self.bases)
        for k, v in self.keywords:
            header_args.append(f"{k}={v}")
        header_parens = f"({', '.join(header_args)})" if header_args else ""
        lines.append(f"{pad}class {self.name}{header_parens}:")
        body_lines: list[str] = []
        if self.docstring is not None:
            body_lines.append(Docstring(self.docstring).render(indent + 1))
        if not self.body and self.docstring is None:
            body_lines.append(_INDENT * (indent + 1) + "pass")
        else:
            for stmt in self.body:
                body_lines.append(stmt.render(indent + 1))
        return "\n".join(lines + body_lines)


@dataclass
class UnknownBlock(Stmt):
    """A region of bytecode the rule engine could not recover.

    `disassembly` is the raw text emitted by `dis` / `xdis`; the LLM pass
    can use it to reconstruct the original source. `signature` describes
    the surrounding context (e.g. function header) so the LLM gets a
    coherent prompt.
    """

    disassembly: str
    signature: str = ""

    def render(self, indent: int = 0) -> str:
        pad = _INDENT * indent
        return f"{pad}pass  # pychd: unrecovered body"


@dataclass
class RawStatement(Stmt):
    """A pre-rendered statement (e.g. produced by the LLM pass)."""

    source: str

    def render(self, indent: int = 0) -> str:
        pad = _INDENT * indent
        return "\n".join(pad + ln if ln else "" for ln in self.source.splitlines())


@dataclass
class If(Stmt):
    """``if <test>:`` block with an optional ``else:`` branch.

    Used to re-emit module-level conditionals — most importantly the
    ubiquitous ``if TYPE_CHECKING:`` typing-only import guard — that
    earlier versions of the walker flattened to top level. ``test`` is
    a pre-rendered expression string; ``body`` and ``orelse`` are
    ordinary statement lists.
    """

    test: str
    body: list[Stmt] = field(default_factory=list)
    orelse: list[Stmt] = field(default_factory=list)

    def render(self, indent: int = 0) -> str:
        pad = _INDENT * indent
        lines: list[str] = [f"{pad}if {self.test}:"]
        if not self.body:
            lines.append(_INDENT * (indent + 1) + "pass")
        else:
            for stmt in self.body:
                lines.append(stmt.render(indent + 1))
        if self.orelse:
            lines.append(f"{pad}else:")
            for stmt in self.orelse:
                lines.append(stmt.render(indent + 1))
        return "\n".join(lines)


@dataclass
class Try(Stmt):
    """``try:`` block with ``except <T>:`` handlers.

    Used to re-emit module-level ``try: import X except ImportError:``
    patterns. ``handlers`` is a list of ``(exception_text, body)``
    tuples; an empty ``exception_text`` renders as a bare ``except:``.
    """

    body: list[Stmt] = field(default_factory=list)
    handlers: list[tuple[str, list[Stmt]]] = field(default_factory=list)

    def render(self, indent: int = 0) -> str:
        pad = _INDENT * indent
        lines: list[str] = [f"{pad}try:"]
        if not self.body:
            lines.append(_INDENT * (indent + 1) + "pass")
        else:
            for stmt in self.body:
                lines.append(stmt.render(indent + 1))
        for exc, handler_body in self.handlers:
            header = f"{pad}except {exc}:" if exc else f"{pad}except:"
            lines.append(header)
            if not handler_body:
                lines.append(_INDENT * (indent + 1) + "pass")
            else:
                for stmt in handler_body:
                    lines.append(stmt.render(indent + 1))
        return "\n".join(lines)


@dataclass
class Module:
    docstring: str | None = None
    body: list[Stmt] = field(default_factory=list)

    def render(self) -> str:
        lines: list[str] = []
        if self.docstring is not None:
            lines.append(Docstring(self.docstring).render(0))
            lines.append("")
        prev: Stmt | None = None
        for stmt in self.body:
            if prev is not None and _needs_blank_line(prev, stmt):
                lines.append("")
            lines.append(stmt.render(0))
            prev = stmt
        if not lines or lines[-1] != "":
            lines.append("")
        return "\n".join(lines)

    def unknown_blocks(self) -> list[UnknownBlock]:
        out: list[UnknownBlock] = []
        for stmt in self.body:
            out.extend(_collect_unknowns(stmt))
        return out


def _collect_unknowns(stmt: Stmt) -> list[UnknownBlock]:
    out: list[UnknownBlock] = []
    if isinstance(stmt, UnknownBlock):
        out.append(stmt)
    elif isinstance(stmt, (FunctionDef, ClassDef)):
        for inner in stmt.body:
            out.extend(_collect_unknowns(inner))
    elif isinstance(stmt, If):
        for inner in stmt.body:
            out.extend(_collect_unknowns(inner))
        for inner in stmt.orelse:
            out.extend(_collect_unknowns(inner))
    elif isinstance(stmt, Try):
        for inner in stmt.body:
            out.extend(_collect_unknowns(inner))
        for _exc, handler in stmt.handlers:
            for inner in handler:
                out.extend(_collect_unknowns(inner))
    return out


def _needs_blank_line(prev: Stmt, curr: Stmt) -> bool:
    """Heuristic for inserting a blank line between top-level statements."""
    heavy = (FunctionDef, ClassDef)
    if isinstance(prev, heavy) or isinstance(curr, heavy):
        return True
    if isinstance(prev, (Import, FromImport)) and not isinstance(
        curr, (Import, FromImport)
    ):
        return True
    return False
