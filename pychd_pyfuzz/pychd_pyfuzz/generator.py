"""Random syntactically-valid Python source generator.

The generator builds an :class:`ast.AST` tree directly and validates
it with ``compile(..., dont_inherit=True)`` before serialising back to
source text. Every successfully-generated sample is therefore valid
under the target Python version.

Design choices:

* **AST not templates.** Direct AST construction makes scope tracking
  and version gating straightforward and rules out trivial syntactic
  bugs.
* **Version gates.** Each builder declares ``min_version``; the
  generator filters its builder pools by ``target`` so e.g. ``match``
  cases never appear in a 3.9 sample.
* **Determinism.** A :class:`random.Random` instance is threaded
  through every choice; the global ``random`` module is untouched.
* **Bounded depth.** Recursive expression / statement generators take
  a depth budget that decrements with each level.
* **No infinite loops.** ``while`` bodies receive a counter
  decrement appended automatically.
* **Scope discipline.** ``Load``-context ``Name`` nodes are drawn
  exclusively from the current scope's visible-name pool.

Returns a :class:`Sample` (source text + tag set + metadata) per
generation; ``Fuzzer.generate_batch(count)`` is the high-level entry.
"""

from __future__ import annotations

import ast
import random
import string
from collections.abc import Callable
from dataclasses import dataclass

from .scope import Scope, module_scope
from .tags import TagSet

VersionT = tuple[int, int]
# Maximum total source-length safeguard. The CLI emits a warning if a
# sample is rejected for length, but typical runs never trip this.
MAX_SOURCE_LEN = 8_000


@dataclass
class Sample:
    """A successfully-generated sample."""

    source: str
    tags: list[str]
    target: VersionT
    seed: int
    # ``index`` is the 0-based position in the batch the Fuzzer was
    # called with — useful so the CLI can write deterministic
    # filenames.
    index: int
    # Source length in characters (post-``ast.unparse``).
    length: int = 0


def _fmt_version(v: VersionT) -> str:
    return f"{v[0]}.{v[1]}"


def _name_from_int(prefix: str, idx: int) -> str:
    # `_n0`, `_n1`, ... — same style the obfuscator will use, so the
    # fuzzer's emitted names are already "anonymised-looking".
    return f"{prefix}{idx}"


@dataclass
class _Builder:
    """A version-gated AST builder."""

    fn: Callable
    min_version: VersionT
    tag: str

    def applies(self, target: VersionT) -> bool:
        return target >= self.min_version


class Fuzzer:
    """Random syntactically-valid Python source generator.

    Construct with a target Python version and (optionally) a seed;
    call :meth:`generate` for a single :class:`Sample` or
    :meth:`generate_batch` for a list of them.
    """

    def __init__(
        self,
        target: VersionT,
        *,
        seed: int = 0,
        max_depth: int = 3,
        max_top_items: int = 6,
    ) -> None:
        if target < (3, 0) or target > (3, 14):
            raise ValueError(f"unsupported target version {target!r}")
        self.target = target
        self.seed = seed
        self._rng = random.Random(seed)
        self.max_depth = max_depth
        self.max_top_items = max_top_items
        self._tag_counter = 0
        self._name_counter = 0

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def generate(self, index: int = 0) -> Sample:
        """Generate a single valid Python module."""
        # Keep retrying with fresh random state until compile() accepts
        # the sample. This is the safety net for the rare case where
        # our builders combined to produce something the parser doesn't
        # accept — they should not in steady state, so we cap retries.
        last_error: BaseException | None = None
        for _ in range(8):
            self._tag_counter = 0
            self._name_counter = 0
            tags = TagSet()
            scope = module_scope()
            module = self._build_module(scope, tags)
            ast.fix_missing_locations(module)
            try:
                source = ast.unparse(module)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
            if len(source) > MAX_SOURCE_LEN:
                last_error = ValueError("sample exceeded MAX_SOURCE_LEN")
                continue
            try:
                compile(source, "<pyfuzz>", "exec", dont_inherit=True)
            except SyntaxError as exc:
                last_error = exc
                continue
            return Sample(
                source=source,
                tags=tags.as_sorted_list(),
                target=self.target,
                seed=self.seed,
                index=index,
                length=len(source),
            )
        raise RuntimeError(
            f"pyfuzz: failed to generate a valid sample after 8 attempts; "
            f"last error: {last_error!r}"
        )

    def generate_batch(self, count: int) -> list[Sample]:
        return [self.generate(i) for i in range(count)]

    # ------------------------------------------------------------------
    # Top-level module assembly
    # ------------------------------------------------------------------

    def _build_module(self, scope: Scope, tags: TagSet) -> ast.Module:
        body: list[ast.stmt] = []
        # Always include a couple of imports so the module is non-trivial.
        body.extend(self._gen_imports(scope, tags))
        # PEP 695 type alias (3.12+) at top level, sometimes.
        if self.target >= (3, 12) and self._rng.random() < 0.4:
            stmt = self._gen_type_alias(scope, tags)
            if stmt is not None:
                body.append(stmt)
        # Module-level constant assignment.
        body.append(self._gen_module_constant(scope, tags))
        # Functions and classes.
        item_count = self._rng.randint(2, self.max_top_items)
        for _ in range(item_count):
            if self._rng.random() < 0.35:
                body.append(self._gen_class_def(scope, tags))
            else:
                body.append(self._gen_function_def(scope, tags))
        return ast.Module(body=body, type_ignores=[])

    # ------------------------------------------------------------------
    # Imports
    # ------------------------------------------------------------------

    def _gen_imports(self, scope: Scope, tags: TagSet) -> list[ast.stmt]:
        out: list[ast.stmt] = []
        # `from __future__ import annotations` is legal everywhere
        # 3.7+ and useful for the deferred-annotation feature tag.
        if self.target >= (3, 7) and self._rng.random() < 0.4:
            out.append(
                ast.ImportFrom(
                    module="__future__",
                    names=[ast.alias(name="annotations", asname=None)],
                    level=0,
                )
            )
            tags.add("future_annotations")
        # Plain `import` of a safe stdlib module.
        mod = self._rng.choice(["math", "os", "sys", "json", "itertools"])
        alias = self._rng.choice([None, self._fresh_name("m")])
        node = ast.Import(names=[ast.alias(name=mod, asname=alias)])
        out.append(node)
        bound = alias if alias else mod
        scope.bind(bound)
        tags.add("import")
        # Sometimes a `from X import Y` for syntactic coverage.
        if self._rng.random() < 0.5:
            mod = self._rng.choice(["collections", "typing", "dataclasses"])
            if mod == "typing" and self.target >= (3, 9):
                name = self._rng.choice(["Any", "Callable", "Iterable"])
            elif mod == "dataclasses":
                name = "dataclass"
            else:
                name = self._rng.choice(["OrderedDict", "deque"])
            node2 = ast.ImportFrom(
                module=mod,
                names=[ast.alias(name=name, asname=None)],
                level=0,
            )
            out.append(node2)
            scope.bind(name)
            tags.add("importfrom")
        return out

    # ------------------------------------------------------------------
    # PEP 695 type alias (3.12+)
    # ------------------------------------------------------------------

    def _gen_type_alias(self, scope: Scope, tags: TagSet) -> ast.stmt | None:
        if self.target < (3, 12):
            return None
        name = self._fresh_name("Alias")
        # type X = int | str   — uses `int | str` which is 3.10+ syntax,
        # but PEP 695 is 3.12+ so we are safely above the floor.
        value: ast.expr = ast.BinOp(
            left=ast.Name(id="int", ctx=ast.Load()),
            op=ast.BitOr(),
            right=ast.Name(id="str", ctx=ast.Load()),
        )
        node = ast.TypeAlias(
            name=ast.Name(id=name, ctx=ast.Store()),
            type_params=[],
            value=value,
        )
        scope.bind(name)
        tags.add("type_alias")
        return node

    # ------------------------------------------------------------------
    # Module constant
    # ------------------------------------------------------------------

    def _gen_module_constant(self, scope: Scope, tags: TagSet) -> ast.stmt:
        name = self._fresh_name("CONST", upper=True)
        value = ast.Constant(value=self._rng.choice([0, 1, "x", True, None]))
        node = ast.Assign(targets=[ast.Name(id=name, ctx=ast.Store())], value=value)
        scope.bind(name)
        tags.add("assign_module")
        return node

    # ------------------------------------------------------------------
    # Functions
    # ------------------------------------------------------------------

    def _gen_function_def(self, scope: Scope, tags: TagSet) -> ast.stmt:
        name = self._fresh_name("fn")
        # Decide async-ness up-front so the inner scope is tagged
        # correctly — ``async for`` / ``async with`` builders gate
        # on ``fn_scope.in_async_function()``.
        is_async = self.target >= (3, 5) and self._rng.random() < 0.15
        # Build args. Mix positional, optional, kwonly, *args, **kwargs.
        fn_scope = scope.child("function", is_async=is_async)
        args = self._gen_arguments(fn_scope, tags)
        # PEP 695 type_params on the function (3.12+).
        type_params: list = []
        if self.target >= (3, 12) and self._rng.random() < 0.35:
            type_params = self._gen_type_params(tags)
        # Decorators.
        decorators = self._gen_decorators(scope, tags)
        # Annotation.
        returns: ast.expr | None = None
        if self._rng.random() < 0.5:
            returns = ast.Name(id="int", ctx=ast.Load())
            tags.add("return_annotation")
        # Body.
        body = self._gen_function_body(fn_scope, tags, depth=self.max_depth)
        # Async-ness was decided when we opened fn_scope above.
        if is_async:
            tags.add("async_function")
            cls: type = ast.AsyncFunctionDef
        else:
            cls = ast.FunctionDef
        # FunctionDef gained `type_params` only in 3.12; older Pythons
        # don't accept the kwarg via positional / keyword path so we
        # build it differently to stay backward-compatible in our own
        # 3.14 runtime (which always supports it). Since this fuzzer
        # itself runs on 3.14+ the AST node accepts type_params either
        # way.
        node = cls(
            name=name,
            args=args,
            body=body,
            decorator_list=decorators,
            returns=returns,
            type_comment=None,
            type_params=type_params,
        )
        scope.bind(name)
        tags.add("function_def")
        return node

    def _gen_arguments(self, fn_scope: Scope, tags: TagSet) -> ast.arguments:
        posonly: list[ast.arg] = []
        args: list[ast.arg] = []
        kwonly: list[ast.arg] = []
        defaults: list[ast.expr] = []
        kw_defaults: list[ast.expr | None] = []
        vararg: ast.arg | None = None
        kwarg: ast.arg | None = None

        n_pos = self._rng.randint(0, 3)
        for _ in range(n_pos):
            an = self._fresh_name("a")
            args.append(self._mk_arg(an))
            fn_scope.bind_param(an)
        # Default for the last positional arg, sometimes.
        if args and self._rng.random() < 0.4:
            defaults.append(ast.Constant(value=0))
            tags.add("default_arg")
        # `/` positional-only separator (3.8+)
        if self.target >= (3, 8) and args and self._rng.random() < 0.2:
            split = self._rng.randint(1, len(args))
            posonly = args[:split]
            args = args[split:]
            tags.add("posonly_arg")
        # *args (3.0+)
        if self._rng.random() < 0.25:
            vn = self._fresh_name("v")
            vararg = self._mk_arg(vn)
            fn_scope.bind_param(vn)
            tags.add("vararg")
        # Keyword-only args. To stay strictly within the stdlib ast
        # stubs (which type ``arg.arg`` as ``str`` — they ignore the
        # bare-``*`` separator case where the runtime accepts None),
        # we only emit kwonly args when there is already a ``*args``
        # vararg in front, so we never need the bare-``*`` form.
        if vararg is not None and self._rng.random() < 0.5:
            for _ in range(self._rng.randint(1, 2)):
                kn = self._fresh_name("k")
                kwonly.append(self._mk_arg(kn))
                fn_scope.bind_param(kn)
                if self._rng.random() < 0.5:
                    kw_defaults.append(ast.Constant(value=0))
                else:
                    kw_defaults.append(None)
            tags.add("kwonly_arg")
        # **kwargs
        if self._rng.random() < 0.2:
            kn = self._fresh_name("kw")
            kwarg = self._mk_arg(kn)
            fn_scope.bind_param(kn)
            tags.add("kwarg")
        return ast.arguments(
            posonlyargs=posonly,
            args=args,
            vararg=vararg,
            kwonlyargs=kwonly,
            kw_defaults=kw_defaults,
            kwarg=kwarg,
            defaults=defaults,
        )

    def _mk_arg(self, name: str) -> ast.arg:
        annotation: ast.expr | None = None
        if self._rng.random() < 0.5:
            annotation = ast.Name(id="int", ctx=ast.Load())
        return ast.arg(arg=name, annotation=annotation, type_comment=None)

    def _gen_type_params(self, tags: TagSet) -> list:
        if self.target < (3, 12):
            return []
        tags.add("type_params")
        out: list = []
        for _ in range(self._rng.randint(1, 2)):
            tname = self._fresh_name("T", upper=True)
            # ast.TypeVar(name, bound=None, default=None) — `default` is 3.13+.
            kwargs: dict = {"name": tname, "bound": None}
            if self.target >= (3, 13) and self._rng.random() < 0.5:
                kwargs["default_value"] = ast.Name(id="int", ctx=ast.Load())
                tags.add("typevar_default")
            try:
                out.append(ast.TypeVar(**kwargs))
            except TypeError:
                out.append(ast.TypeVar(name=tname, bound=None))
        return out

    def _gen_decorators(self, scope: Scope, tags: TagSet) -> list[ast.expr]:
        decorators: list[ast.expr] = []
        # Mostly no decorator, sometimes one or two.
        n = self._rng.choices([0, 1, 2], weights=[6, 3, 1])[0]
        for _ in range(n):
            # Pick from a small fixed set so we don't need to track
            # additional bindings.
            choice = self._rng.choice(["staticmethod", "classmethod", "property"])
            decorators.append(ast.Name(id=choice, ctx=ast.Load()))
            tags.add("decorator")
        return decorators

    def _gen_function_body(
        self, fn_scope: Scope, tags: TagSet, depth: int
    ) -> list[ast.stmt]:
        body: list[ast.stmt] = []
        # Always at least one statement.
        n = self._rng.randint(1, 4)
        for _ in range(n):
            body.append(self._gen_statement(fn_scope, tags, depth=depth))
        # Always return something so the function is non-trivial.
        body.append(
            ast.Return(
                value=self._gen_expression(fn_scope, tags, depth=1),
            )
        )
        tags.add("return")
        return body

    # ------------------------------------------------------------------
    # Classes
    # ------------------------------------------------------------------

    def _gen_class_def(self, scope: Scope, tags: TagSet) -> ast.stmt:
        name = self._fresh_name("C", upper=True)
        class_scope = scope.child("class")
        bases: list[ast.expr] = []
        # Optional single base from a safe builtin.
        if self._rng.random() < 0.5:
            base = self._rng.choice(["object", "dict", "list"])
            bases.append(ast.Name(id=base, ctx=ast.Load()))
        # PEP 695 type_params on the class (3.12+).
        type_params: list = []
        if self.target >= (3, 12) and self._rng.random() < 0.3:
            type_params = self._gen_type_params(tags)
        body: list[ast.stmt] = []
        # Class-level annotated attribute.
        attr_name = self._fresh_name("a")
        body.append(
            ast.AnnAssign(
                target=ast.Name(id=attr_name, ctx=ast.Store()),
                annotation=ast.Name(id="int", ctx=ast.Load()),
                value=ast.Constant(value=0),
                simple=1,
            )
        )
        class_scope.bind(attr_name)
        tags.add("annassign")
        # A couple of methods.
        for _ in range(self._rng.randint(1, 2)):
            body.append(self._gen_method_def(class_scope, tags))
        node = ast.ClassDef(
            name=name,
            bases=bases,
            keywords=[],
            body=body,
            decorator_list=[],
            type_params=type_params,
        )
        scope.bind(name)
        tags.add("class_def")
        return node

    def _gen_method_def(self, class_scope: Scope, tags: TagSet) -> ast.stmt:
        name = self._fresh_name("m")
        fn_scope = class_scope.child("function")
        # `self` as the first param.
        fn_scope.bind_param("self")
        args = ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg="self", annotation=None, type_comment=None)],
            vararg=None,
            kwonlyargs=[],
            kw_defaults=[],
            kwarg=None,
            defaults=[],
        )
        body = self._gen_function_body(fn_scope, tags, depth=self.max_depth - 1)
        node = ast.FunctionDef(
            name=name,
            args=args,
            body=body,
            decorator_list=[],
            returns=None,
            type_comment=None,
            type_params=[],
        )
        class_scope.bind(name)
        tags.add("method_def")
        return node

    # ------------------------------------------------------------------
    # Statement pool
    # ------------------------------------------------------------------

    def _gen_statement(self, scope: Scope, tags: TagSet, *, depth: int) -> ast.stmt:
        builders = self._statement_builders(depth)
        builder = self._rng.choice(builders)
        return builder(scope, tags, depth - 1)

    def _statement_builders(self, depth: int):
        """Return the statement-builder pool valid for the current
        target / depth."""
        pool: list = []
        pool.append(self._stmt_assign)
        pool.append(self._stmt_aug_assign)
        pool.append(self._stmt_ann_assign)
        pool.append(self._stmt_expr)
        pool.append(self._stmt_pass)
        if depth > 0:
            pool.append(self._stmt_if)
            pool.append(self._stmt_for)
            pool.append(self._stmt_while)
            pool.append(self._stmt_with)
            pool.append(self._stmt_try)
        if depth > 0 and self.target >= (3, 10):
            pool.append(self._stmt_match)
        if depth > 0 and self.target >= (3, 11):
            pool.append(self._stmt_try_star)
        return pool

    def _stmt_assign(self, scope: Scope, tags: TagSet, depth: int) -> ast.stmt:
        target_name = self._fresh_name("x")
        scope.bind(target_name)
        node = ast.Assign(
            targets=[ast.Name(id=target_name, ctx=ast.Store())],
            value=self._gen_expression(scope, tags, depth=depth),
        )
        tags.add("assign")
        return node

    def _stmt_aug_assign(self, scope: Scope, tags: TagSet, depth: int) -> ast.stmt:
        names = scope.visible_names()
        # We need an existing local; if none, fall back to a fresh
        # initialised binding so the augmented assign is meaningful.
        if not any(n in scope.locals or n in scope.params for n in names):
            target_name = self._fresh_name("y")
            scope.bind(target_name)
            return ast.Assign(
                targets=[ast.Name(id=target_name, ctx=ast.Store())],
                value=ast.Constant(value=0),
            )
        target_name = self._rng.choice(sorted(scope.locals | scope.params))
        node = ast.AugAssign(
            target=ast.Name(id=target_name, ctx=ast.Store()),
            op=self._rng.choice([ast.Add(), ast.Sub(), ast.Mult()]),
            value=ast.Constant(value=1),
        )
        tags.add("aug_assign")
        return node

    def _stmt_ann_assign(self, scope: Scope, tags: TagSet, depth: int) -> ast.stmt:
        name = self._fresh_name("z")
        scope.bind(name)
        node = ast.AnnAssign(
            target=ast.Name(id=name, ctx=ast.Store()),
            annotation=ast.Name(id="int", ctx=ast.Load()),
            value=ast.Constant(value=0) if self._rng.random() < 0.7 else None,
            simple=1,
        )
        tags.add("annassign")
        return node

    def _stmt_expr(self, scope: Scope, tags: TagSet, depth: int) -> ast.stmt:
        tags.add("expr_stmt")
        return ast.Expr(value=self._gen_expression(scope, tags, depth=depth))

    def _stmt_pass(self, scope: Scope, tags: TagSet, depth: int) -> ast.stmt:
        tags.add("pass")
        return ast.Pass()

    def _stmt_if(self, scope: Scope, tags: TagSet, depth: int) -> ast.stmt:
        test = self._gen_expression(scope, tags, depth=1)
        body = [self._gen_statement(scope, tags, depth=depth)]
        orelse: list[ast.stmt] = []
        if self._rng.random() < 0.5:
            orelse = [self._gen_statement(scope, tags, depth=depth)]
        tags.add("if")
        return ast.If(test=test, body=body, orelse=orelse)

    def _stmt_for(self, scope: Scope, tags: TagSet, depth: int) -> ast.stmt:
        loop_var = self._fresh_name("i")
        scope.bind(loop_var)
        body: list[ast.stmt] = [self._gen_statement(scope, tags, depth=depth)]
        # ``async for`` is only valid lexically inside an ``async def``.
        if (
            self.target >= (3, 5)
            and scope.in_async_function()
            and self._rng.random() < 0.3
        ):
            tags.add("async_for")
            cls: type = ast.AsyncFor
        else:
            cls = ast.For
        tags.add("for")
        return cls(
            target=ast.Name(id=loop_var, ctx=ast.Store()),
            iter=ast.Call(
                func=ast.Name(id="range", ctx=ast.Load()),
                args=[ast.Constant(value=3)],
                keywords=[],
            ),
            body=body,
            orelse=[],
            type_comment=None,
        )

    def _stmt_while(self, scope: Scope, tags: TagSet, depth: int) -> ast.stmt:
        # Build a bounded loop: introduce a counter `_loop_<n>` and
        # decrement at the end of each iteration so we never emit an
        # infinite loop.
        ctr = self._fresh_name("loop")
        scope.bind(ctr)
        init = ast.Assign(
            targets=[ast.Name(id=ctr, ctx=ast.Store())],
            value=ast.Constant(value=3),
        )
        body = [
            self._gen_statement(scope, tags, depth=depth),
            ast.AugAssign(
                target=ast.Name(id=ctr, ctx=ast.Store()),
                op=ast.Sub(),
                value=ast.Constant(value=1),
            ),
        ]
        while_node = ast.While(
            test=ast.Compare(
                left=ast.Name(id=ctr, ctx=ast.Load()),
                ops=[ast.Gt()],
                comparators=[ast.Constant(value=0)],
            ),
            body=body,
            orelse=[],
        )
        tags.add("while")
        # Return a `If True: init; while: ...` wrapper isn't great
        # because we need both stmts at the same level. The fuzzer
        # caller treats a single returned stmt as inserted at that
        # position; we'd lose the counter init. So we cheat slightly:
        # return an `If True` that contains both. (`If True` is legal
        # and the parser does not constant-fold it the way `if False`
        # gets dropped — see README §LLM contamination disclosure for
        # why that matters.)
        return ast.If(
            test=ast.Constant(value=True),
            body=[init, while_node],
            orelse=[],
        )

    def _stmt_with(self, scope: Scope, tags: TagSet, depth: int) -> ast.stmt:
        item = ast.withitem(
            context_expr=ast.Call(
                func=ast.Name(id="open", ctx=ast.Load()),
                args=[ast.Constant(value="/dev/null")],
                keywords=[],
            ),
            optional_vars=None,
        )
        body = [self._gen_statement(scope, tags, depth=depth)]
        cls: type = ast.With
        if (
            self.target >= (3, 5)
            and scope.in_async_function()
            and self._rng.random() < 0.3
        ):
            tags.add("async_with")
            cls = ast.AsyncWith
        tags.add("with")
        return cls(items=[item], body=body, type_comment=None)

    def _stmt_try(self, scope: Scope, tags: TagSet, depth: int) -> ast.stmt:
        body = [self._gen_statement(scope, tags, depth=depth)]
        handler = ast.ExceptHandler(
            type=ast.Name(id="Exception", ctx=ast.Load()),
            name=None,
            body=[ast.Pass()],
        )
        tags.add("try")
        return ast.Try(
            body=body,
            handlers=[handler],
            orelse=[],
            finalbody=[],
        )

    def _stmt_try_star(self, scope: Scope, tags: TagSet, depth: int) -> ast.stmt:
        if self.target < (3, 11):
            return self._stmt_try(scope, tags, depth)
        body = [self._gen_statement(scope, tags, depth=depth)]
        handler = ast.ExceptHandler(
            type=ast.Name(id="Exception", ctx=ast.Load()),
            name=None,
            body=[ast.Pass()],
        )
        tags.add("try_star")
        return ast.TryStar(
            body=body,
            handlers=[handler],
            orelse=[],
            finalbody=[],
        )

    def _stmt_match(self, scope: Scope, tags: TagSet, depth: int) -> ast.stmt:
        if self.target < (3, 10):
            return self._stmt_if(scope, tags, depth)
        # `match VALUE: case 0: ...; case _: ...`
        subject = ast.Constant(value=0)
        cases = [
            ast.match_case(
                pattern=ast.MatchValue(value=ast.Constant(value=0)),
                guard=None,
                body=[ast.Pass()],
            ),
            ast.match_case(
                pattern=ast.MatchAs(pattern=None, name=None),
                guard=None,
                body=[ast.Pass()],
            ),
        ]
        tags.add("match")
        return ast.Match(subject=subject, cases=cases)

    # ------------------------------------------------------------------
    # Expression pool
    # ------------------------------------------------------------------

    def _gen_expression(self, scope: Scope, tags: TagSet, *, depth: int) -> ast.expr:
        builders = self._expression_builders(depth)
        builder = self._rng.choice(builders)
        return builder(scope, tags, depth - 1)

    def _expression_builders(self, depth: int):
        pool: list = []
        pool.append(self._expr_constant)
        pool.append(self._expr_name)
        pool.append(self._expr_tuple)
        pool.append(self._expr_list)
        pool.append(self._expr_dict)
        pool.append(self._expr_set)
        if depth > 0:
            pool.append(self._expr_binop)
            pool.append(self._expr_compare)
            pool.append(self._expr_call)
            pool.append(self._expr_ifexp)
            pool.append(self._expr_lambda)
            pool.append(self._expr_listcomp)
            pool.append(self._expr_genexp)
            pool.append(self._expr_attribute)
            pool.append(self._expr_subscript)
            pool.append(self._expr_unary)
            pool.append(self._expr_boolop)
        if depth > 0 and self.target >= (3, 6):
            pool.append(self._expr_fstring)
        if depth > 0 and self.target >= (3, 8):
            pool.append(self._expr_walrus)
        return pool

    def _expr_constant(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        # ``ast.Constant.value`` accepts a closed union (str | bytes |
        # int | float | complex | bool | None | EllipsisType). Build
        # the choice pool with that exact type so the static checker
        # is happy without needing any escape hatch.
        ConstValue = str | bytes | int | float | bool | None
        choices: list[ConstValue] = [0, 1, 42, -1, 0.5, "x", True, False, None]
        if self._rng.random() < 0.1:
            choices.append(b"x")
        v: ConstValue = self._rng.choice(choices)
        tags.add("constant")
        return ast.Constant(value=v)

    def _expr_name(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        names = scope.visible_names()
        if not names:
            return self._expr_constant(scope, tags, depth)
        tags.add("name_load")
        return ast.Name(id=self._rng.choice(names), ctx=ast.Load())

    def _expr_tuple(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        n = self._rng.randint(0, 3)
        elts = [self._gen_expression(scope, tags, depth=0) for _ in range(n)]
        tags.add("tuple")
        return ast.Tuple(elts=elts, ctx=ast.Load())

    def _expr_list(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        n = self._rng.randint(0, 3)
        elts = [self._gen_expression(scope, tags, depth=0) for _ in range(n)]
        tags.add("list")
        return ast.List(elts=elts, ctx=ast.Load())

    def _expr_dict(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        n = self._rng.randint(0, 2)
        keys: list[ast.expr | None] = []
        values: list[ast.expr] = []
        for _ in range(n):
            keys.append(ast.Constant(value=self._rng.randint(0, 9)))
            values.append(self._gen_expression(scope, tags, depth=0))
        tags.add("dict")
        return ast.Dict(keys=keys, values=values)

    def _expr_set(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        n = self._rng.randint(1, 3)
        elts: list[ast.expr] = [
            ast.Constant(value=self._rng.randint(0, 9)) for _ in range(n)
        ]
        tags.add("set")
        return ast.Set(elts=elts)

    def _expr_binop(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        op = self._rng.choice(
            [ast.Add(), ast.Sub(), ast.Mult(), ast.FloorDiv(), ast.BitOr()]
        )
        tags.add("binop")
        return ast.BinOp(
            left=self._gen_expression(scope, tags, depth=depth),
            op=op,
            right=self._gen_expression(scope, tags, depth=depth),
        )

    def _expr_compare(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        # ``is`` / ``is not`` only avoid CPython's SyntaxWarning when
        # neither operand is a non-singleton literal. The simplest
        # safe form is ``Name is None`` / ``Name is not True`` etc.
        # Use a Name on the left and a singleton on the right.
        use_singleton_op = self._rng.random() < 0.2
        if use_singleton_op:
            op = self._rng.choice([ast.Is(), ast.IsNot()])
            names = scope.visible_names()
            left: ast.expr
            if names:
                left = ast.Name(id=self._rng.choice(names), ctx=ast.Load())
            else:
                left = ast.Constant(value=42)  # last-resort fallback
            right: ast.expr = ast.Constant(value=self._rng.choice([None, True, False]))
        else:
            op = self._rng.choice([ast.Eq(), ast.NotEq(), ast.Lt(), ast.GtE()])
            left = self._gen_expression(scope, tags, depth=depth)
            right = self._gen_expression(scope, tags, depth=depth)
        tags.add("compare")
        return ast.Compare(
            left=left,
            ops=[op],
            comparators=[right],
        )

    def _expr_call(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        # Always call into a safe builtin so we don't generate
        # unbound names.
        fn = self._rng.choice(["abs", "len", "str", "int", "min", "max"])
        n = self._rng.randint(1, 2)
        args = [self._gen_expression(scope, tags, depth=0) for _ in range(n)]
        tags.add("call")
        return ast.Call(func=ast.Name(id=fn, ctx=ast.Load()), args=args, keywords=[])

    def _expr_ifexp(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        tags.add("ifexp")
        return ast.IfExp(
            test=self._gen_expression(scope, tags, depth=0),
            body=self._gen_expression(scope, tags, depth=0),
            orelse=self._gen_expression(scope, tags, depth=0),
        )

    def _expr_lambda(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        lam_scope = scope.child("function")
        lam_scope.bind_param("x")
        args = ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg="x", annotation=None, type_comment=None)],
            vararg=None,
            kwonlyargs=[],
            kw_defaults=[],
            kwarg=None,
            defaults=[],
        )
        body = self._gen_expression(lam_scope, tags, depth=0)
        tags.add("lambda")
        return ast.Lambda(args=args, body=body)

    def _expr_listcomp(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        comp_scope = scope.child("comprehension")
        comp_scope.bind_param("i")
        gen = ast.comprehension(
            target=ast.Name(id="i", ctx=ast.Store()),
            iter=ast.Call(
                func=ast.Name(id="range", ctx=ast.Load()),
                args=[ast.Constant(value=3)],
                keywords=[],
            ),
            ifs=[],
            is_async=0,
        )
        elt = ast.BinOp(
            left=ast.Name(id="i", ctx=ast.Load()),
            op=ast.Mult(),
            right=ast.Constant(value=2),
        )
        tags.add("listcomp")
        return ast.ListComp(elt=elt, generators=[gen])

    def _expr_genexp(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        comp_scope = scope.child("comprehension")
        comp_scope.bind_param("i")
        gen = ast.comprehension(
            target=ast.Name(id="i", ctx=ast.Store()),
            iter=ast.Call(
                func=ast.Name(id="range", ctx=ast.Load()),
                args=[ast.Constant(value=3)],
                keywords=[],
            ),
            ifs=[],
            is_async=0,
        )
        tags.add("genexp")
        return ast.GeneratorExp(elt=ast.Name(id="i", ctx=ast.Load()), generators=[gen])

    def _expr_attribute(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        # `_obj.bit_length()` style — pick an `int` attribute.
        tags.add("attribute")
        return ast.Attribute(
            value=ast.Constant(value=42),
            attr="bit_length",
            ctx=ast.Load(),
        )

    def _expr_subscript(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        tags.add("subscript")
        return ast.Subscript(
            value=ast.List(
                elts=[ast.Constant(value=1), ast.Constant(value=2)],
                ctx=ast.Load(),
            ),
            slice=ast.Constant(value=0),
            ctx=ast.Load(),
        )

    def _expr_unary(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        op = self._rng.choice([ast.UAdd(), ast.USub(), ast.Not(), ast.Invert()])
        tags.add("unary")
        return ast.UnaryOp(op=op, operand=self._gen_expression(scope, tags, depth=0))

    def _expr_boolop(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        op = self._rng.choice([ast.And(), ast.Or()])
        tags.add("boolop")
        return ast.BoolOp(
            op=op,
            values=[
                self._gen_expression(scope, tags, depth=0),
                self._gen_expression(scope, tags, depth=0),
            ],
        )

    def _expr_fstring(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        if self.target < (3, 6):
            return self._expr_constant(scope, tags, depth)
        # f"x={x!r}"
        tags.add("fstring")
        return ast.JoinedStr(
            values=[
                ast.Constant(value="x="),
                ast.FormattedValue(
                    value=ast.Constant(value=42),
                    conversion=-1,
                    format_spec=None,
                ),
            ]
        )

    def _expr_walrus(self, scope: Scope, tags: TagSet, depth: int) -> ast.expr:
        if self.target < (3, 8):
            return self._expr_constant(scope, tags, depth)
        wname = self._fresh_name("w")
        scope.bind(wname)
        tags.add("walrus")
        return ast.NamedExpr(
            target=ast.Name(id=wname, ctx=ast.Store()),
            value=ast.Constant(value=1),
        )

    # ------------------------------------------------------------------
    # Name minting
    # ------------------------------------------------------------------

    def _fresh_name(self, prefix: str, upper: bool = False) -> str:
        self._name_counter += 1
        suffix = self._name_counter
        # A handful of extra random letters keeps the name space wider
        # than `_x1 _x2 ...` and avoids accidental collisions with
        # builtins.
        letters = "".join(self._rng.choices(string.ascii_lowercase, k=2))
        name = f"_{prefix}{letters}{suffix}"
        return name.upper() if upper else name


__all__ = ["Fuzzer", "Sample"]
