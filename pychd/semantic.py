"""Five-axis *semantic* + *similarity* equivalence between an original
``.pyc`` and a recovered ``.py``.

The rest of the benchmark suite measures recovery against the ``.py``
ground truth via AST comparison (``signature_match`` /
``declaration_match`` / ``strict_match`` in :mod:`tools.benchmark`).
That tells us how close the recovered source *reads* to the original.
It does **not** tell us whether the recovered source *means* the same
thing once the compiler is done with it.

This module adds the complementary axes: take the recovered ``.py``,
re-compile or re-execute it under the **same Python interpreter that
produced the original .pyc**, and compare the two artefacts at five
levels of strictness — three bytecode/runtime axes ("semantic"), one
test-driven correctness axis (Pass@1), plus a continuous text-level
similarity axis (edit distance).

The five axes:

``functional_correctness`` (Pass@1)
    For corpora that ship an oracle (HumanEval is the canonical
    example), re-runs the original test ``check(candidate)`` against
    the recovered module's entry-point function and reports whether
    the assertion suite passes. This is the strongest possible claim
    — the recovered code is *behaviourally* equivalent to the original
    on the corpus's own test inputs. Used by Decompile-Bench
    (arXiv 2505.12668) and PyLingual (USENIX Security 2025) as the
    canonical re-executability oracle. Returns ``None`` when no test
    data is available for the module.

``edit_similarity``
    Character-level normalised Levenshtein-style similarity in
    ``[0, 1]`` via :class:`difflib.SequenceMatcher`. Comparable to
    the "Edit Similarity" metric reported in Decompile-Bench. A
    continuous metric — unlike every other axis, this one returns a
    *score* rather than a boolean, which is useful for tracking small
    incremental rule-pass improvements that don't yet flip any
    boolean axis.

The remaining three axes are bytecode/runtime booleans:

``behavioral_smoke``
    Subprocess-imports both modules under the original interpreter
    and compares (a) the set of public names and (b) the
    ``inspect.signature`` of every public callable. This is the
    *strongest semantic claim* a non-executing comparator can make
    on arbitrary code: the recovered module presents the same API
    surface. It tolerates compiler normalisations completely —
    docstring whitespace, constant folding, op specialisation — but
    catches recovered modules that fail to import, lose names, or
    change argument signatures.

``bytecode_normalized``
    Recursively walks both code objects, gathers each one's
    instruction stream via :func:`dis.get_instructions`, and
    compares ``(opname, argval)`` sequences after canonicalising
    away CACHE / NOP / RESUME bookkeeping, specialised opcode
    variants, and the small handful of opcodes whose ``argval`` is
    a nested ``code`` object (we recurse into those). This is the
    sweet spot for "did the rule pass round-trip *semantically*?"
    in practice — strong enough to catch most real regressions but
    not punished by reordered constants or cosmetic compiler
    normalisations.

``bytecode_exact``
    Compares ``marshal.dumps(orig_code) == marshal.dumps(new_code)``.
    The strictest of the three, with the highest false-negative
    rate. Any change in ``co_consts`` ordering, ``co_lnotab`` /
    ``co_positions``, exception table layout, or specialised
    opcode emission will trip it. We report it for completeness
    and as an upper bound: if ``bytecode_exact`` holds,
    ``bytecode_normalized`` is guaranteed to hold and the recovered
    source is bit-identical at the bytecode level.

All three comparators take the **original ``.pyc`` path** as input
because we need its magic number to identify the producing interpreter.
The recovered source is re-compiled via the same interpreter (out of
process — never the current interpreter, which may be a different
3.x release) so that the comparison is apples-to-apples.

Why not pyc → py → pyc by hand?
-------------------------------

Each axis tolerates a specific class of compiler-induced false
negative. A naïve ``marshal.dumps`` byte comparison conflates "the
source changed" with five unrelated phenomena:

1. ``co_firstlineno`` / ``co_lnotab`` / ``co_positions`` drift from
   any whitespace or comment difference;
2. ``co_consts`` / ``co_names`` / ``co_varnames`` reordering when the
   compiler folds or re-emits expressions;
3. CPython 3.11+ specialising-interpreter adaptive opcodes
   (``LOAD_FAST_CHECK`` / ``LOAD_FAST_BORROW`` / ``LOAD_FAST_AND_CLEAR``);
4. exception-table layout (PEP 657) for try/except blocks that compile
   to identical control flow;
5. magic-number mismatch across minor versions — fixed here by always
   recompiling with the original interpreter.

``bytecode_normalized`` strips (1) wholesale, (3) by mapping
specialised opcodes back to their base form, and (4) by ignoring
``CACHE`` and friends. It cannot defeat (2) because the compiler
*assigns* indices into ``co_consts`` from instruction operands, so we
compare the resolved ``argval`` instead of the index. ``behavioral``
defeats everything above by only observing the recovered module's
*surface*.
"""

from __future__ import annotations

import difflib
import dis
import json
import logging
import marshal
import py_compile
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from types import CodeType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class SemanticResult:
    """Outcome of a single semantic comparator.

    ``match`` is the headline boolean. ``detail`` carries an
    explanation (the failing instruction pair, the first divergent
    public name, an exception message, etc.) that survives into the
    benchmark JSON so reviewers can audit individual misses without
    re-running anything.
    """

    match: bool
    detail: str = ""


# ---------------------------------------------------------------------------
# Magic-number → interpreter
# ---------------------------------------------------------------------------


def _read_pyc_magic(pyc: Path) -> int:
    """Return the 16-bit magic number from the .pyc header."""
    with open(pyc, "rb") as f:
        head = f.read(4)
    if len(head) < 4:
        raise ValueError(f"{pyc}: truncated .pyc header")
    return struct.unpack("<H", head[:2])[0]


def _read_code_from_pyc(pyc: Path) -> CodeType:
    """Load the top-level ``code`` object from a .pyc the *current*
    interpreter produced. Raises ``ValueError`` if the magic differs.

    The .pyc header layout is:

    - 4 bytes  magic number
    - 4 bytes  flags (PEP 552)
    - 8 bytes  hash or (mtime, size)
    - rest     marshalled code object
    """
    import importlib.util

    our_magic = importlib.util.MAGIC_NUMBER
    with open(pyc, "rb") as f:
        head_magic = f.read(4)
        if head_magic != our_magic:
            raise ValueError(
                f"{pyc}: magic mismatch ({head_magic!r} != {our_magic!r}); "
                "load via a subprocess of the producing interpreter instead."
            )
        f.read(12)  # flags (4) + hash/timestamp+size (8)
        return marshal.loads(f.read())


def _python_for_magic(magic: int) -> str | None:
    """Best-effort: locate a ``python<version>`` that emits *magic*.

    We rely on the project-internal magic table in :mod:`pychd.versions`
    to translate the .pyc magic to a Python release string, then ask
    ``uv python find <version>`` for an absolute interpreter path.
    Returns ``None`` if uv cannot satisfy the request (the caller will
    skip the bytecode-level axes for that module).
    """
    from pychd.versions import detect_version

    # detect_version takes a Path; we synthesise a minimal 4-byte
    # header so it can dispatch on the magic alone.
    with tempfile.NamedTemporaryFile(suffix=".pyc", delete=False) as tmp:
        tmp.write(struct.pack("<HH", magic, 0x0A0D))
        tmp.write(b"\x00" * 12)
        tmp_path = Path(tmp.name)
    try:
        info = detect_version(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    version = info.label  # e.g. "3.8" / "3.14"
    if not version or version == "unknown":
        return None
    try:
        proc = subprocess.run(
            ["uv", "python", "find", version],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError, subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip().splitlines()
    return out[0] if out else None


# ---------------------------------------------------------------------------
# Bytecode-exact
# ---------------------------------------------------------------------------


def _compile_recovered(
    py_interp: str | None, recovered_src: str, *, workdir: Path
) -> Path | None:
    """Compile *recovered_src* into a .pyc using *py_interp* (or the
    current interpreter when *py_interp* is ``None``). Returns the
    output .pyc path, or ``None`` if compilation failed.

    Errors raised by ``py_compile`` (e.g. invalid escape sequences,
    syntax errors that survived ``ast.parse`` because they're warnings
    only) are caught and surfaced via the ``None`` return — the caller
    records the comparator as a non-match rather than aborting the
    benchmark.
    """
    src = workdir / "recovered.py"
    pyc = workdir / "recovered.pyc"
    src.write_text(recovered_src)
    if py_interp is None or py_interp == sys.executable:
        try:
            py_compile.compile(str(src), cfile=str(pyc), doraise=True)
        except Exception as e:
            logger.debug("py_compile (in-process) failed: %s", e)
            return None
        return pyc
    snippet = textwrap.dedent(
        f"""\
        import py_compile, sys
        try:
            py_compile.compile({str(src)!r}, cfile={str(pyc)!r}, doraise=True)
        except Exception as e:
            sys.stderr.write(repr(e))
            sys.exit(1)
        """
    )
    try:
        proc = subprocess.run(
            [py_interp, "-c", snippet],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug("py_compile (subprocess) crashed: %s", e)
        return None
    if proc.returncode != 0:
        logger.debug("py_compile (subprocess) non-zero: %s", proc.stderr)
        return None
    return pyc


def _marshal_bytes(pyc: Path) -> bytes | None:
    """Strip the .pyc header and return the marshalled code-object
    payload. ``None`` on read failure."""
    try:
        with open(pyc, "rb") as f:
            f.read(16)
            return f.read()
    except OSError:
        return None


def _normalize_code_filename(co: CodeType, name: str = "<source>") -> CodeType:
    """Return *co* with ``co_filename`` rewritten to *name*, recursively
    for every nested code object in ``co_consts``.

    A .pyc embeds the producing path inside every code object's
    ``co_filename``. That value is irrelevant to the bytecode's
    *semantics* but defeats ``marshal.dumps`` equality whenever the
    original and recompiled source live at different paths (which is
    always the case in the benchmark: original is a real file, recovered
    is a fresh tempfile). Normalising both sides to the same sentinel
    string lets ``bytecode_exact`` reflect what its name claims.
    """
    new_consts = tuple(
        _normalize_code_filename(c, name) if isinstance(c, CodeType) else c
        for c in co.co_consts
    )
    return co.replace(co_filename=name, co_consts=new_consts)


def _marshal_normalized(pyc: Path) -> bytes | None:
    """Read *pyc*, normalise ``co_filename`` away, and marshal the result.

    Returns ``None`` on read or marshal failure.
    """
    try:
        with open(pyc, "rb") as f:
            f.read(16)
            co = marshal.loads(f.read())
    except OSError, ValueError:
        return None
    return marshal.dumps(_normalize_code_filename(co))


def bytecode_exact(
    pyc: Path,
    recovered_src: str,
    *,
    py_interp: str | None = None,
) -> SemanticResult:
    """Byte-for-byte equality of ``marshal.dumps(code)``.

    Requires *recovered_src* to compile under *py_interp* (defaulting
    to the magic-number-derived interpreter). Returns a non-match with
    explanatory ``detail`` whenever any of the steps fail rather than
    propagating the exception.
    """
    try:
        if py_interp is None:
            py_interp = _python_for_magic(_read_pyc_magic(pyc))
    except ValueError as e:
        return SemanticResult(False, str(e))
    if py_interp is None:
        return SemanticResult(False, "no interpreter for magic")

    # Same interpreter → in-process marshal compare with co_filename
    # normalised away.
    if py_interp == sys.executable:
        orig_bytes = _marshal_normalized(pyc)
        if orig_bytes is None:
            return SemanticResult(False, "cannot read original .pyc payload")
        with tempfile.TemporaryDirectory() as tmp:
            new_pyc = _compile_recovered(py_interp, recovered_src, workdir=Path(tmp))
            if new_pyc is None:
                return SemanticResult(False, "recovered source failed to compile")
            new_bytes = _marshal_normalized(new_pyc)
            if new_bytes is None:
                return SemanticResult(False, "cannot read recompiled .pyc payload")
            if orig_bytes == new_bytes:
                return SemanticResult(True, "marshal payload identical")
            return SemanticResult(
                False,
                f"marshal payload differs "
                f"({len(orig_bytes)} vs {len(new_bytes)} bytes)",
            )

    # Cross-version → marshal.loads must run under the producing
    # interpreter (different .pyc magic = different marshal format).
    # Delegate the whole compare to a subprocess.
    with tempfile.TemporaryDirectory() as tmp:
        src_path = Path(tmp) / "recovered.py"
        src_path.write_text(recovered_src)
        snippet = textwrap.dedent(
            f"""\
            import json, marshal, py_compile, sys
            from types import CodeType

            def normalize(co):
                new_consts = tuple(
                    normalize(c) if isinstance(c, CodeType) else c
                    for c in co.co_consts
                )
                return co.replace(co_filename="<source>", co_consts=new_consts)

            def load(pyc):
                with open(pyc, "rb") as f:
                    f.read(16)
                    return marshal.loads(f.read())

            try:
                orig = load({str(pyc)!r})
            except Exception as e:
                print(json.dumps({{
                    "ok": False, "detail": f"orig load failed: {{e!r}}"
                }}))
                sys.exit(0)
            try:
                py_compile.compile(
                    {str(src_path)!r},
                    cfile={str(Path(tmp) / "rec.pyc")!r},
                    doraise=True,
                )
            except Exception as e:
                print(json.dumps({{
                    "ok": False, "detail": f"py_compile failed: {{e!r}}"
                }}))
                sys.exit(0)
            try:
                new = load({str(Path(tmp) / "rec.pyc")!r})
            except Exception as e:
                print(json.dumps({{
                    "ok": False, "detail": f"new load failed: {{e!r}}"
                }}))
                sys.exit(0)
            a, b = marshal.dumps(normalize(orig)), marshal.dumps(normalize(new))
            if a == b:
                print(json.dumps({{"ok": True, "detail": "marshal identical"}}))
            else:
                print(json.dumps({{
                    "ok": False,
                    "detail": f"marshal differs ({{len(a)}} vs {{len(b)}} bytes)",
                }}))
            """
        )
        try:
            proc = subprocess.run(
                [py_interp, "-c", snippet],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return SemanticResult(False, f"subprocess crashed: {e}")
        if proc.returncode != 0:
            return SemanticResult(False, f"subprocess non-zero: {proc.stderr.strip()}")
        try:
            data = json.loads(proc.stdout.strip().splitlines()[-1])
        except ValueError, IndexError:
            return SemanticResult(False, f"bad subprocess output: {proc.stdout!r}")
        return SemanticResult(bool(data.get("ok")), str(data.get("detail", "")))


# ---------------------------------------------------------------------------
# Bytecode-normalized
# ---------------------------------------------------------------------------


# Specialised / adaptive opcode variants introduced by the CPython 3.11+
# specialising interpreter. ``dis`` already de-specialises some of them
# via ``dis.get_instructions(..., adaptive=False)`` but we belt-and-brace
# the mapping for the variants that are visible at compile time
# (i.e. emitted by the compiler, not patched in by the interpreter).
_OPCODE_ALIASES: dict[str, str] = {
    # 3.13+ borrow-aware fast-locals — semantically a plain LOAD_FAST
    "LOAD_FAST_BORROW": "LOAD_FAST",
    "LOAD_FAST_CHECK": "LOAD_FAST",
    "LOAD_FAST_AND_CLEAR": "LOAD_FAST",
    # 3.13: a fused LOAD_CONST for small ints
    "LOAD_SMALL_INT": "LOAD_CONST",
    # 3.12: RETURN_CONST collapses LOAD_CONST + RETURN_VALUE.
    # Map it back so a recovered file that emits the two-instruction
    # form still compares equal to a compiler that emits the fused one
    # (and vice versa). The caller flattens the resulting sequence so
    # the synthetic LOAD_CONST shows up alongside an explicit
    # RETURN_VALUE downstream.
    "RETURN_CONST": "RETURN_VALUE",
}

# Opcodes emitted purely as bookkeeping by the compiler/interpreter.
# Stripping them lets recovered source that the compiler placed
# CACHE/NOP instructions around still compare equal.
_SKIP_OPCODES = frozenset(
    {
        "CACHE",
        "NOP",
        "RESUME",
        "PRECALL",  # 3.11; collapsed into CALL in 3.12
        "EXTENDED_ARG",
        # KW_NAMES (3.12+) is essentially a hint to the next CALL.
        # Its argval is reflected in the CALL's argval already.
        "KW_NAMES",
    }
)


def _canonical_instructions(
    co: CodeType, *, _seen: set[int] | None = None
) -> list[tuple[str, object]]:
    """Recursively flatten *co* into a canonical instruction list.

    Each entry is ``(opname, argval)`` where:

    - ``opname`` is mapped through :data:`_OPCODE_ALIASES`;
    - ``argval`` is the resolved value (the string name, the constant,
      or the recursive canonical form of a nested code object);
    - ``CACHE`` / ``NOP`` / ``RESUME`` / ``PRECALL`` / ``EXTENDED_ARG``
      / ``KW_NAMES`` are dropped.

    The cycle guard via ``_seen`` is defensive — code objects don't
    form cycles in normal Python but we want to be robust against
    unusual marshal payloads.
    """
    if _seen is None:
        _seen = set()
    if id(co) in _seen:
        return [("__CYCLE__", None)]
    _seen.add(id(co))

    out: list[tuple[str, object]] = []
    for ins in dis.get_instructions(co):
        if ins.opname in _SKIP_OPCODES:
            continue
        opname = _OPCODE_ALIASES.get(ins.opname, ins.opname)

        argval: object
        # RETURN_CONST → emit synthetic LOAD_CONST, then RETURN_VALUE,
        # so a recovered "LOAD_CONST x; RETURN_VALUE" matches the
        # compiler's fused form (and vice versa).
        if ins.opname == "RETURN_CONST":
            out.append(("LOAD_CONST", _canonical_argval(ins.argval, _seen)))
            argval = None
        else:
            argval = _canonical_argval(ins.argval, _seen)
        out.append((opname, argval))
    return out


def _canonical_argval(argval: object, seen: set[int]) -> object:
    """Recursively canonicalise an instruction's ``argval``.

    Nested code objects compare by their own canonical instruction
    list. Tuples/lists/frozensets that contain code objects (the
    only place this happens in practice is ``MAKE_FUNCTION`` /
    annotation closures) are mapped element-wise.
    """
    if isinstance(argval, CodeType):
        return ("__CODE__", argval.co_name, _canonical_instructions(argval, _seen=seen))
    if isinstance(argval, (tuple, list)):
        return tuple(_canonical_argval(x, seen) for x in argval)
    if isinstance(argval, frozenset):
        return frozenset(_canonical_argval(x, seen) for x in argval)
    return argval


def bytecode_normalized(
    pyc: Path,
    recovered_src: str,
    *,
    py_interp: str | None = None,
) -> SemanticResult:
    """Compare *pyc* and *recovered_src* by canonical instruction stream.

    When *py_interp* differs from the current interpreter the comparison
    runs out of process: a small Python snippet does the loading,
    canonicalisation, and equality check on the producing-interpreter
    side and prints a JSON envelope back. This is the only way to be
    sure ``dis.get_instructions`` is interpreting the bytecode under
    the same opcode table that emitted it.
    """
    try:
        if py_interp is None:
            py_interp = _python_for_magic(_read_pyc_magic(pyc))
    except ValueError as e:
        return SemanticResult(False, str(e))
    if py_interp is None:
        return SemanticResult(False, "no interpreter for magic")

    # Same interpreter → do it in-process. We can safely import
    # :func:`_canonical_instructions` against the current ``dis``.
    if py_interp == sys.executable:
        try:
            co = _read_code_from_pyc(pyc)
        except ValueError as e:
            return SemanticResult(False, str(e))
        with tempfile.TemporaryDirectory() as tmp:
            new_pyc = _compile_recovered(py_interp, recovered_src, workdir=Path(tmp))
            if new_pyc is None:
                return SemanticResult(False, "recovered source failed to compile")
            try:
                new_co = _read_code_from_pyc(new_pyc)
            except ValueError as e:
                return SemanticResult(False, str(e))
        a = _canonical_instructions(co)
        b = _canonical_instructions(new_co)
        if a == b:
            return SemanticResult(True, "canonical instruction stream identical")
        # Cheap first-diff localisation — helps when debugging benchmark
        # outliers without re-running the comparator under a debugger.
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                return SemanticResult(
                    False, f"first divergence at #{i}: {x!r} vs {y!r}"
                )
        return SemanticResult(False, f"length mismatch: orig={len(a)}, new={len(b)}")

    # Different interpreter → do the entire comparison out of process.
    with tempfile.TemporaryDirectory() as tmp:
        src_path = Path(tmp) / "recovered.py"
        src_path.write_text(recovered_src)
        snippet = textwrap.dedent(
            f"""\
            import dis, json, marshal, sys, py_compile
            from pathlib import Path
            from types import CodeType

            SKIP = {{
                "CACHE", "NOP", "RESUME",
                "PRECALL", "EXTENDED_ARG", "KW_NAMES",
            }}
            ALIASES = {{
                "LOAD_FAST_BORROW": "LOAD_FAST",
                "LOAD_FAST_CHECK": "LOAD_FAST",
                "LOAD_FAST_AND_CLEAR": "LOAD_FAST",
                "LOAD_SMALL_INT": "LOAD_CONST",
                "RETURN_CONST": "RETURN_VALUE",
            }}

            def canon_arg(v, seen):
                if isinstance(v, CodeType):
                    return ["__CODE__", v.co_name, canon(v, seen)]
                if isinstance(v, (tuple, list)):
                    return [canon_arg(x, seen) for x in v]
                if isinstance(v, frozenset):
                    return ["__FS__", sorted(repr(x) for x in v)]
                try:
                    json.dumps(v)
                    return v
                except TypeError:
                    return repr(v)

            def canon(co, seen=None):
                seen = seen if seen is not None else set()
                if id(co) in seen:
                    return [["__CYCLE__", None]]
                seen.add(id(co))
                out = []
                for ins in dis.get_instructions(co):
                    if ins.opname in SKIP:
                        continue
                    name = ALIASES.get(ins.opname, ins.opname)
                    if ins.opname == "RETURN_CONST":
                        out.append(["LOAD_CONST", canon_arg(ins.argval, seen)])
                        out.append([name, None])
                    else:
                        out.append([name, canon_arg(ins.argval, seen)])
                return out

            def load(pyc):
                with open(pyc, "rb") as f:
                    f.read(16)
                    return marshal.loads(f.read())

            orig = load({str(pyc)!r})
            new_pyc = Path({str(tmp)!r}) / "recovered.pyc"
            try:
                py_compile.compile({str(src_path)!r}, cfile=str(new_pyc), doraise=True)
            except Exception as e:
                print(json.dumps({{"ok": False, "detail": f"py_compile: {{e}}"}}))
                sys.exit(0)
            new = load(new_pyc)
            a, b = canon(orig), canon(new)
            if a == b:
                print(json.dumps({{"ok": True, "detail": "identical"}}))
            else:
                first = None
                for i, (x, y) in enumerate(zip(a, b)):
                    if x != y:
                        first = [i, x, y]
                        break
                print(json.dumps({{
                    "ok": False,
                    "detail": (
                        f"first divergence at #{{first[0]}}: "
                        f"{{first[1]}} vs {{first[2]}}"
                    ) if first else f"length mismatch: {{len(a)}} vs {{len(b)}}",
                }}))
            """
        )
        try:
            proc = subprocess.run(
                [py_interp, "-c", snippet],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return SemanticResult(False, f"subprocess crashed: {e}")
        if proc.returncode != 0:
            return SemanticResult(False, f"subprocess non-zero: {proc.stderr.strip()}")
        try:
            data = json.loads(proc.stdout.strip().splitlines()[-1])
        except ValueError, IndexError:
            return SemanticResult(False, f"bad subprocess output: {proc.stdout!r}")
        return SemanticResult(bool(data.get("ok")), str(data.get("detail", "")))


# ---------------------------------------------------------------------------
# Behavioral smoke
# ---------------------------------------------------------------------------


_BEHAVIORAL_PROBE = textwrap.dedent(
    """\
    import importlib.util, inspect, json, sys
    from pathlib import Path

    def public_surface(mod):
        out = {}
        for name in dir(mod):
            if name.startswith("_"):
                continue
            obj = getattr(mod, name, None)
            kind = type(obj).__name__
            sig = None
            if callable(obj):
                try:
                    sig = str(inspect.signature(obj))
                except (TypeError, ValueError):
                    sig = "<unsignable>"
            out[name] = {"kind": kind, "sig": sig}
        return out

    def load(path, name):
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    orig_path, rec_path = sys.argv[1], sys.argv[2]

    def try_import(path, name):
        # First try a plain import. If that fails we'll fall back to
        # adding the file's parent directory to sys.path — this fixes
        # cursor-sdk-style sister imports without exposing the corpus
        # dir to every other module's resolver (which regressed BS on
        # stdlib modules when their re-exports started picking up
        # other corpus copies instead of the real stdlib).
        try:
            return load(path, name), None
        except Exception as exc:
            parent = str(Path(path).parent)
            added = parent not in sys.path
            if added:
                sys.path.insert(0, parent)
            try:
                return load(path, name), None
            except Exception as exc2:
                if added:
                    sys.path.remove(parent)
                return None, exc2 if exc2 is not None else exc

    orig, err = try_import(orig_path, "_pychd_orig")
    if orig is None:
        print(json.dumps({"ok": False, "detail": f"orig import failed: {err!r}"}))
        sys.exit(0)
    rec, err = try_import(rec_path, "_pychd_rec")
    if rec is None:
        print(json.dumps({"ok": False, "detail": f"recovered import failed: {err!r}"}))
        sys.exit(0)
    a, b = public_surface(orig), public_surface(rec)
    missing = sorted(set(a) - set(b))
    if missing:
        print(json.dumps({
            "ok": False,
            "detail": f"recovered missing {len(missing)} name(s); first: {missing[0]}",
        }))
        sys.exit(0)
    sig_diffs = []
    for name, info in a.items():
        if info["sig"] is None or info["sig"] == "<unsignable>":
            continue
        rec_info = b.get(name, {})
        if rec_info.get("sig") not in (info["sig"], "<unsignable>"):
            sig_diffs.append((name, info["sig"], rec_info.get("sig")))
    if sig_diffs:
        n, want, got = sig_diffs[0]
        print(json.dumps({
            "ok": False,
            "detail": (
                f"{len(sig_diffs)} signature diff(s); first: "
                f"{n}: {want!r} vs {got!r}"
            ),
        }))
        sys.exit(0)
    print(json.dumps({"ok": True, "detail": f"{len(a)} public names match"}))
    """
)


def behavioral_smoke(
    orig_py: Path,
    recovered_src: str,
    *,
    py_interp: str | None = None,
    timeout: float = 20.0,
) -> SemanticResult:
    """Import both modules in a subprocess and compare their public API.

    We deliberately *do not* execute callables — too many corpora have
    import-time side effects already, and invoking arbitrary recovered
    functions would mean running pychd's output without a sandbox.
    What we *do* check is the strongest claim that a non-executing
    comparator can make:

    1. The recovered source imports cleanly under the producing
       interpreter (catches recovered modules that ``ast.parse`` accepts
       but ``exec`` rejects — e.g. forward references to names the
       rule pass failed to extract).
    2. Every public top-level name (``not name.startswith("_")``)
       survives.
    3. Every public callable has the same ``inspect.signature`` (this
       defeats compiler-driven decorator stripping that bypasses
       ``ast.parse``-level comparators).

    Caller supplies the path to the *original ``.py``* (not the
    ``.pyc``): we need to import the original under its own interpreter
    too, and importing a .pyc the running interpreter can't read is
    fiddly.
    """
    interp = py_interp or sys.executable
    if not shutil.which(interp) and not Path(interp).is_file():
        return SemanticResult(False, f"interpreter not executable: {interp}")
    with tempfile.TemporaryDirectory() as tmp:
        rec = Path(tmp) / "recovered.py"
        rec.write_text(recovered_src)
        probe = Path(tmp) / "probe.py"
        probe.write_text(_BEHAVIORAL_PROBE)
        try:
            proc = subprocess.run(
                [interp, str(probe), str(orig_py), str(rec)],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return SemanticResult(False, f"subprocess crashed: {e}")
    out = proc.stdout.strip().splitlines()
    if not out:
        return SemanticResult(False, f"probe produced no output: {proc.stderr!r}")
    try:
        data = json.loads(out[-1])
    except ValueError:
        return SemanticResult(False, f"probe output not JSON: {out[-1]!r}")
    return SemanticResult(bool(data.get("ok")), str(data.get("detail", "")))


# ---------------------------------------------------------------------------
# Edit similarity (continuous text-level metric)
# ---------------------------------------------------------------------------


# Cap the inputs handed to ``SequenceMatcher`` so a 2 MB pypi module
# doesn't push a single benchmark row into the seconds-per-module
# bracket. SequenceMatcher is O(n*m) in the worst case; on 100 KB
# inputs it returns in well under 100 ms on modern hardware, which is
# the operating point we want for a per-module metric.
_EDIT_SIMILARITY_CAP = 100_000


def edit_similarity(orig_src: str, recovered_src: str) -> float:
    """Character-level normalised similarity in ``[0, 1]``.

    Wraps :func:`difflib.SequenceMatcher.ratio` — the Python stdlib's
    ratcliff-obershelp variant of edit-distance-normalised-by-length.
    Identical inputs return 1.0; entirely different inputs trend
    towards 0.0. This is the standard "Edit Similarity" metric used in
    Decompile-Bench (arXiv 2505.12668) for comparing decompiler output
    against ground truth.

    Inputs longer than :data:`_EDIT_SIMILARITY_CAP` are truncated at
    both ends to keep the per-module cost bounded. The cap is large
    enough that every module in the headline corpus fits whole.
    """
    if orig_src == recovered_src:
        return 1.0
    if not orig_src and not recovered_src:
        return 1.0
    a = orig_src[:_EDIT_SIMILARITY_CAP]
    b = recovered_src[:_EDIT_SIMILARITY_CAP]
    return difflib.SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# Functional correctness (Pass@1)
# ---------------------------------------------------------------------------


_FUNCTIONAL_PROBE_TEMPLATE = textwrap.dedent(
    """\
    import importlib.util, json, sys, traceback

    def _load(path, name):
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError("cannot spec " + str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    try:
        _rec = _load({rec_path!r}, "_pychd_rec")
    except Exception as e:
        print(json.dumps({{
            "ok": False,
            "detail": "recovered import failed: " + repr(e),
        }}))
        sys.exit(0)

    try:
        candidate = getattr(_rec, {entry_point!r})
    except AttributeError as e:
        print(json.dumps({{
            "ok": False,
            "detail": "missing entry point: " + repr(e),
        }}))
        sys.exit(0)

    # --- begin test script (verbatim from corpus oracle) ---
    {test_src}
    # --- end test script ---

    try:
        check(candidate)
    except AssertionError as e:
        print(json.dumps({{
            "ok": False,
            "detail": "assertion failed: " + str(e),
        }}))
        sys.exit(0)
    except Exception as e:
        print(json.dumps({{
            "ok": False,
            "detail": (
                "check raised " + type(e).__name__ + ": " + str(e)
            ),
        }}))
        sys.exit(0)
    print(json.dumps({{"ok": True, "detail": "check passed"}}))
    """
)


def functional_correctness(
    recovered_src: str,
    test_src: str,
    entry_point: str,
    *,
    py_interp: str | None = None,
    timeout: float = 10.0,
) -> SemanticResult:
    """Pass@1: run *test_src*'s ``check(candidate)`` against the recovered
    module's *entry_point*.

    *test_src* is treated as a verbatim Python snippet that defines a
    top-level ``check`` callable taking one argument (the candidate
    function). HumanEval ships exactly this shape via its ``test``
    field, which is why the parameter is named that way.

    Subprocess-isolated: the recovered module never executes under
    pychd's own interpreter context, and the test snippet can raise,
    loop, or import freely without contaminating the benchmark host.
    Timeout is enforced for protection against accidental infinite
    loops in recovered code.

    Returns a non-match with explanatory ``detail`` for every failure
    mode (import failure, missing entry point, assertion failure,
    arbitrary exception, subprocess crash) — the caller renders these
    verbatim into the per-module JSON for audit.
    """
    if test_src is None or entry_point is None:
        return SemanticResult(False, "no test data")
    interp = py_interp or sys.executable
    # The template expects ``test_src`` at column 0 — dedent so any
    # accidental indentation in the corpus oracle (HumanEval ships
    # tests with a leading blank line; some MBPP-style corpora indent
    # to match a prompt) doesn't break the spliced script.
    flat_test = textwrap.dedent(test_src)
    with tempfile.TemporaryDirectory() as tmp:
        rec = Path(tmp) / "recovered.py"
        rec.write_text(recovered_src)
        probe = Path(tmp) / "probe.py"
        probe.write_text(
            _FUNCTIONAL_PROBE_TEMPLATE.format(
                rec_path=str(rec),
                entry_point=entry_point,
                test_src=flat_test,
            )
        )
        try:
            proc = subprocess.run(
                [interp, str(probe)],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return SemanticResult(False, f"subprocess crashed: {e}")
    out = proc.stdout.strip().splitlines()
    if not out:
        stderr = proc.stderr.strip()[:200] if proc.stderr else "(no stderr)"
        return SemanticResult(False, f"probe produced no output: {stderr}")
    try:
        data = json.loads(out[-1])
    except ValueError:
        return SemanticResult(False, f"probe output not JSON: {out[-1]!r}")
    return SemanticResult(bool(data.get("ok")), str(data.get("detail", "")))


# ---------------------------------------------------------------------------
# Combined entry point used by the benchmark
# ---------------------------------------------------------------------------


@dataclass
class SemanticReport:
    """Result of running all comparators against the same pair.

    Each axis can independently succeed or fail; the report is what
    the benchmark serialises into per-module JSON. The two new fields
    (``functional_correctness`` / ``edit_similarity``) align pychd's
    metrics with the published Python-decompilation benchmarks
    (Decompile-Bench, PyLingual) — see the module docstring for the
    paper mapping.

    ``functional_correctness`` is ``None`` when no test oracle is
    available for the module (the common case outside HumanEval).
    """

    bytecode_exact: SemanticResult
    bytecode_normalized: SemanticResult
    behavioral_smoke: SemanticResult
    edit_similarity: float = 0.0
    functional_correctness: SemanticResult | None = None
    # Reserve future extension fields rather than re-shuffling the
    # dataclass on every metric addition.
    extras: dict[str, object] = field(default_factory=dict)


def compare_all(
    pyc: Path,
    orig_py: Path,
    recovered_src: str,
    *,
    py_interp: str | None = None,
    orig_src: str | None = None,
    test_src: str | None = None,
    entry_point: str | None = None,
) -> SemanticReport:
    """Run all comparators against ``pyc`` / ``recovered_src``.

    The interpreter is resolved once and reused so that every axis is
    answered consistently against the same producing CPython.

    When ``test_src`` and ``entry_point`` are supplied, the Pass@1 axis
    runs and the report's ``functional_correctness`` is populated.
    Callers that don't have test data (every corpus besides HumanEval)
    leave both at ``None`` — the field stays ``None`` in the report.

    ``orig_src`` is used for the edit-similarity axis. When omitted,
    it's read from ``orig_py``.
    """
    if py_interp is None:
        try:
            py_interp = _python_for_magic(_read_pyc_magic(pyc))
        except ValueError:
            py_interp = None
    if orig_src is None:
        try:
            orig_src = orig_py.read_text()
        except OSError:
            orig_src = ""
    fc: SemanticResult | None = None
    if test_src is not None and entry_point is not None:
        fc = functional_correctness(
            recovered_src, test_src, entry_point, py_interp=py_interp
        )
    return SemanticReport(
        bytecode_exact=bytecode_exact(pyc, recovered_src, py_interp=py_interp),
        bytecode_normalized=bytecode_normalized(
            pyc, recovered_src, py_interp=py_interp
        ),
        behavioral_smoke=behavioral_smoke(orig_py, recovered_src, py_interp=py_interp),
        edit_similarity=edit_similarity(orig_src, recovered_src),
        functional_correctness=fc,
    )
