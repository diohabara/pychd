"""Cross-version .pyc anonymiser via subprocess into the target Python.

The native rewriter (``rewrite_native``) only works for .pyc files
produced by the currently-running interpreter — ``types.CodeType``
internals (e.g. ``co_qualname`` availability, exception-table layout
on 3.11+, the older ``co_lnotab`` shape on 3.10-) differ across
minors and the ``replace`` kwarg surface must match.

To stay version-agnostic without re-implementing every layout, this
module spawns the *writer*'s Python interpreter under ``uv run
--python 3.X --no-project python -c "<snippet>"`` and runs the same
``marshal.loads → recursive replace → marshal.dumps`` dance inside
that subprocess. The snippet is the multi-line string at the bottom
of this file, kept as a plain ``str`` so it is easy to read and
review.

Constraints:

* Communication is via three file-paths passed on argv (input .pyc,
  output .pyc, mapping JSON). No piping marshalled bytes through
  stdin/stdout — keeps the snippet trivial and avoids encoding
  issues across 3.x.
* ``uv`` is the only required tooling — it manages downloading the
  target Python on first run via python-build-standalone. Hosts
  without uv get a clear ``FileNotFoundError`` instead of a
  baffling ``subprocess`` traceback.
* 30-second wall-clock timeout per call, matching the cross-version
  fixture builder (``tools/build_multiversion_fixtures.py``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .rewrite_native import ObfuscationMapping


def _snippet() -> str:
    """Return the subprocess script as a single-string ``-c`` body.

    The snippet uses only standard library modules available in every
    Python 3.x release (``marshal`` / ``types`` / ``json`` / ``sys``)
    so we do not need to install anything inside the target venv.
    """
    return r"""
import json
import marshal
import sys
from pathlib import Path

# argv layout: in_pyc, out_pyc, mapping_json, header_len
in_pyc = Path(sys.argv[1])
out_pyc = Path(sys.argv[2])
mapping_path = Path(sys.argv[3])
header_len = int(sys.argv[4])

data = in_pyc.read_bytes()
header = data[:header_len]
body = data[header_len:]
code = marshal.loads(body)

mapping = {
    "names": {},
    "varnames": {},
    "freevars": {},
    "cellvars": {},
    "consts": {},
    "co_names": {},
}


def _anon_tuple(seq, prefix, table):
    out = []
    for i, name in enumerate(seq):
        if name in table:
            out.append(table[name])
            continue
        new = "_" + prefix + str(i)
        table[name] = new
        out.append(new)
    return tuple(out)


def _anon_const(c):
    if isinstance(c, str):
        if c in mapping["consts"]:
            return mapping["consts"][c]
        new = "_s" + str(len(mapping["consts"]))
        mapping["consts"][c] = new
        return new
    if isinstance(c, tuple):
        return tuple(_anon_const(item) for item in c)
    if isinstance(c, frozenset):
        return frozenset(_anon_const(item) for item in c)
    if type(c).__name__ == "code":  # CodeType
        return _anon_code(c, depth + 1)  # noqa: F821 — depth bound at outer scope
    return c


_depth_counters = {}


def _anon_code(code, depth):
    # NOTE: ``_anon_const`` references ``depth`` via closure on each
    # entry to ``_anon_code`` — we rebind it at each level by
    # assigning a fresh inner function. This keeps the script under
    # 60 LOC and avoids passing depth through the const recursion.
    global _anon_const

    def _anon_const(c, _d=depth):
        if isinstance(c, str):
            if c in mapping["consts"]:
                return mapping["consts"][c]
            new = "_s" + str(len(mapping["consts"]))
            mapping["consts"][c] = new
            return new
        if isinstance(c, tuple):
            return tuple(_anon_const(item) for item in c)
        if isinstance(c, frozenset):
            return frozenset(_anon_const(item) for item in c)
        if type(c).__name__ == "code":
            return _anon_code(c, _d + 1)
        return c

    new_names = _anon_tuple(code.co_names, "n", mapping["names"])
    new_varnames = _anon_tuple(code.co_varnames, "v", mapping["varnames"])
    new_freevars = _anon_tuple(code.co_freevars, "f", mapping["freevars"])
    new_cellvars = _anon_tuple(code.co_cellvars, "c", mapping["cellvars"])

    new_consts = tuple(_anon_const(c) for c in code.co_consts)

    n_at_depth = _depth_counters.get(depth, 0)
    new_co_name = "_fn" + str(depth) + "_" + str(n_at_depth)
    _depth_counters[depth] = n_at_depth + 1
    mapping["co_names"][code.co_name] = new_co_name

    kwargs = dict(
        co_names=new_names,
        co_varnames=new_varnames,
        co_freevars=new_freevars,
        co_cellvars=new_cellvars,
        co_consts=new_consts,
        co_name=new_co_name,
        co_filename="<anonymised>",
        co_firstlineno=1,
    )
    if hasattr(code, "co_qualname"):
        kwargs["co_qualname"] = new_co_name
    if hasattr(code, "co_linetable"):
        kwargs["co_linetable"] = b""
    # co_lnotab is the line-table kwarg only on 3.10 and earlier; on
    # 3.11+ it exists as a deprecated read-only alias and code.replace
    # rejects it.
    if hasattr(code, "co_lnotab") and sys.version_info < (3, 11):
        kwargs["co_lnotab"] = b""
    return code.replace(**kwargs)


new_code = _anon_code(code, 0)
new_body = marshal.dumps(new_code)
out_pyc.write_bytes(header + new_body)
mapping_path.write_text(json.dumps(mapping))
"""


def run_subprocess_rewrite(
    target_python: str,
    in_pyc: Path,
    out_pyc: Path,
    header_len: int,
    *,
    timeout: float = 30.0,
) -> ObfuscationMapping:
    """Spawn *target_python* and rewrite *in_pyc* into *out_pyc*.

    *target_python* is the command/path that, when executed, runs the
    correct Python minor. The standard form on this repo is
    ``uv run --python 3.X --no-project python`` — call sites pass
    that as a single string (or a list joined with spaces) and the
    function dispatches via ``shlex`` if necessary.

    The mapping JSON is written to a temp file next to *out_pyc* and
    parsed back here so the caller receives a fully-populated
    :class:`ObfuscationMapping`.
    """
    import shlex
    import tempfile

    out_pyc.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="pyobf-map-", suffix=".json", delete=False
    ) as fh:
        mapping_path = Path(fh.name)
    cmd = shlex.split(target_python) + [
        "-c",
        _snippet(),
        str(in_pyc),
        str(out_pyc),
        str(mapping_path),
        str(header_len),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        mapping_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"pyobf subprocess (cmd={cmd[:4]!r}) failed: "
            f"rc={proc.returncode}, stderr={proc.stderr!r}"
        )
    raw = json.loads(mapping_path.read_text())
    mapping_path.unlink(missing_ok=True)
    om = ObfuscationMapping()
    om.names.update(raw["names"])
    om.varnames.update(raw["varnames"])
    om.freevars.update(raw["freevars"])
    om.cellvars.update(raw["cellvars"])
    om.consts.update(raw["consts"])
    om.co_names.update(raw["co_names"])
    return om


def uv_run_command(version: tuple[int, int]) -> str:
    """Return the ``uv``-mediated command string that runs the target
    Python without any project dependencies.

    Centralised here so the dispatcher and the eval-harness both call
    it the same way (and so the test suite can monkey-patch it when
    running offline).
    """
    return f"uv run --python {version[0]}.{version[1]} --no-project python"


__all__ = ["run_subprocess_rewrite", "uv_run_command"]
