"""Hybrid Python bytecode decompiler.

Pipeline:

1. Disassemble the .pyc via xdis (cross-version) and, when the .pyc
   was produced by the current interpreter, load the module's code
   object via stdlib ``marshal``.
2. Run the rule-based extractor (`pychd.rules`) to recover the module
   skeleton — imports, function/class signatures, docstrings, simple
   constants — into an IR (`pychd.ir.Module`).
3. For every ``UnknownBlock`` left in the IR, query the LLM with the
   *signature* and the disassembly of *just that body*. The LLM's
   output is spliced back into the IR.
4. Render the IR back to source.

Modes:

- *rules-only* — skip the LLM entirely. Bodies become
  ``pass  # pychd: unrecovered body``.
- *llm-only* — bypass rules and feed the full disassembly to the LLM
  (matches pychd's original behaviour).
- *hybrid* (default) — rules first, LLM fills the gaps.

Cross-version: the rule engine handles only the *current* interpreter
version (Python 3.14 today). Older .pyc files compiled by a different
Python fall back to ``llm-only`` automatically.
"""

from __future__ import annotations

import dis
import io
import logging
import marshal
import sys
import textwrap
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import CodeType

import litellm
from litellm import completion, token_counter
from xdis.disasm import disco
from xdis.load import load_module

from pychd import ir, rules
from pychd.types import ModelType


class Mode(str, Enum):
    HYBRID = "hybrid"
    RULES_ONLY = "rules-only"
    LLM_ONLY = "llm-only"


@dataclass
class DecompileReport:
    source: str
    mode: Mode
    rule_confidence: float | None
    unknown_blocks: int
    llm_calls: int
    version_tuple: tuple


def _disassemble_native(pyc_file: Path) -> tuple[str, CodeType]:
    """Disassemble using the standard library (current interpreter only)."""
    with open(pyc_file, "rb") as f:
        f.read(16)  # 16-byte .pyc header in 3.7+
        bytecode = marshal.load(f)
    string_output = io.StringIO()
    original_stdout = sys.stdout
    sys.stdout = string_output
    dis.dis(bytecode)
    sys.stdout = original_stdout
    return string_output.getvalue(), bytecode


def disassemble_pyc_file(pyc_file: Path) -> tuple[str, tuple, CodeType | None]:
    """Disassemble a .pyc file from any Python version.

    Returns ``(disassembled_text, version_tuple, code_or_none)``.
    ``code_or_none`` is the stdlib ``CodeType`` when the .pyc matches
    the running interpreter (which lets the rule pass run); otherwise
    it is ``None`` and we use xdis text-only output for the LLM path.
    """
    (
        version_tuple,
        timestamp,
        magic_int,
        co,
        is_pypy,
        source_size,
        sip_hash,
    ) = load_module(str(pyc_file))
    logging.debug(f"{version_tuple=}, {magic_int=}, {is_pypy=}")
    logging.info(
        f"Detected Python {version_tuple[0]}.{version_tuple[1]} bytecode"
        f"{' (PyPy)' if is_pypy else ''}"
    )

    try:
        string_output = io.StringIO()
        disco(
            version_tuple,
            co,
            timestamp,
            out=string_output,
            is_pypy=is_pypy,
            magic_int=magic_int,
            source_size=source_size,
            sip_hash=sip_hash,
        )
        disassembled_pyc = string_output.getvalue()
    except Exception:
        py_version = sys.version_info[:2]
        if version_tuple[:2] == py_version:
            logging.debug("xdis failed, falling back to standard dis module")
            disassembled_pyc, _ = _disassemble_native(pyc_file)
        else:
            raise

    native_code: CodeType | None = None
    py_version = sys.version_info[:2]
    if version_tuple[:2] == py_version:
        try:
            _, native_code = _disassemble_native(pyc_file)
        except Exception:
            native_code = None

    logging.debug(
        textwrap.dedent(
            f"""\
              ⭐⭐⭐ Disassembled Python bytecode STARTS ⭐⭐⭐
              {disassembled_pyc}
              ⭐⭐⭐ Disassembled Python bytecode ENDS ⭐⭐⭐
              """
        )
    )
    return disassembled_pyc, version_tuple, native_code


def _get_max_input_tokens(model: str, default: int = 8192) -> int:
    try:
        info = litellm.get_model_info(model)
        max_tokens = info["max_input_tokens"]
        if max_tokens is not None:
            return max_tokens
        return default
    except Exception:
        logging.debug(
            f"Could not retrieve model info for {model!r}; using default {default}"
        )
        return default


def _split_disassembly(text: str, max_tokens: int, model: str) -> list[str]:
    blocks = text.split("\n\n")
    chunks: list[str] = []
    current_blocks: list[str] = []
    current_text = ""
    for block in blocks:
        candidate = current_text + "\n\n" + block if current_text else block
        candidate_tokens = token_counter(model=model, text=candidate)
        if candidate_tokens <= max_tokens or not current_blocks:
            current_blocks.append(block)
            current_text = candidate
        else:
            chunks.append(current_text)
            current_blocks = [block]
            current_text = block
    if current_text:
        chunks.append(current_text)
    return chunks


def _llm_decompile_chunk(
    disassembled_pyc: str,
    version_tuple: tuple,
    model: ModelType,
    part_info: str | None = None,
) -> str:
    version_str = f"{version_tuple[0]}.{version_tuple[1]}"
    part_line = f"\nThis is {part_info} of the disassembly.\n" if part_info else ""
    user_prompt = textwrap.dedent(
        f"""\
        You are a Python decompiler.
        You will be given a disassembled Python {version_str} bytecode.
        Decompile it into the original Python {version_str} source code.
        Output only the original full source code.
        Do not the natural language description.
        Do not surround the code with triple quotes such as '```' or '```python'.
        {part_line}```
        {disassembled_pyc}
        ```
        """
    )
    response = completion(  # pyrefly: ignore[not-callable]
        model=model,
        temperature=0.7,
        messages=[{"role": "user", "content": user_prompt}],
    )
    logging.debug(f"{response=}")
    generated_text: str = response.choices[0].message.content
    logging.debug(f"{generated_text=}")
    return generated_text


def _llm_fill_body(
    signature: str,
    disassembly: str,
    version_tuple: tuple,
    model: ModelType,
) -> str:
    """Ask the LLM to reconstruct a single function/method body."""
    version_str = f"{version_tuple[0]}.{version_tuple[1]}"
    user_prompt = textwrap.dedent(
        f"""\
        You are a Python decompiler.
        The following Python {version_str} bytecode is the body of:
            {signature}
        Reconstruct the original Python source for *just the body*,
        as one or more statements, indented with 4 spaces.
        Do not include the `def` / `async def` / `class` header line.
        Do not include surrounding triple-quoted fences.
        Output only the body lines.

        ```
        {disassembly}
        ```
        """
    )
    response = completion(  # pyrefly: ignore[not-callable]
        model=model,
        temperature=0.2,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text: str = response.choices[0].message.content
    return _clean_llm_body(text, signature)


def _clean_llm_body(text: str, signature: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    sig_prefix = signature.split("(")[0].strip()
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        if sig_prefix and line.strip().startswith(sig_prefix):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).rstrip()


def decompile_disassembled_pyc(
    disassembled_pyc: str, version_tuple: tuple, model: ModelType
) -> str:
    """LLM-only path. Kept for backward compatibility."""
    logging.info(f"{model=}")
    prompt_overhead = 2000
    max_input = _get_max_input_tokens(model) - prompt_overhead
    chunks = _split_disassembly(disassembled_pyc, max_tokens=max_input, model=model)
    if len(chunks) == 1:
        return _llm_decompile_chunk(disassembled_pyc, version_tuple, model)
    logging.info(f"Disassembly split into {len(chunks)} chunks")
    parts: list[str] = []
    for idx, chunk in enumerate(chunks, 1):
        part_info = f"Part {idx} of {len(chunks)}"
        logging.info(f"Decompiling {part_info}...")
        parts.append(
            _llm_decompile_chunk(chunk, version_tuple, model, part_info=part_info)
        )
    return "\n\n".join(parts)


def _fill_module_with_llm(
    module: ir.Module,
    version_tuple: tuple,
    model: ModelType,
) -> int:
    calls = 0
    for stmt in module.body:
        calls += _fill_stmt_with_llm(stmt, version_tuple, model)
    return calls


def _fill_stmt_with_llm(
    stmt: ir.Stmt,
    version_tuple: tuple,
    model: ModelType,
) -> int:
    if isinstance(stmt, (ir.FunctionDef, ir.ClassDef)):
        calls = 0
        new_body: list[ir.Stmt] = []
        for inner in stmt.body:
            if isinstance(inner, ir.UnknownBlock):
                sig = inner.signature or (
                    f"<{type(stmt).__name__}.{getattr(stmt, 'name', '?')}>"
                )
                body_src = _llm_fill_body(sig, inner.disassembly, version_tuple, model)
                if not body_src.strip():
                    body_src = "pass"
                new_body.append(ir.RawStatement(source=body_src))
                calls += 1
            elif isinstance(inner, (ir.FunctionDef, ir.ClassDef)):
                calls += _fill_stmt_with_llm(inner, version_tuple, model)
                new_body.append(inner)
            else:
                new_body.append(inner)
        stmt.body = new_body
        return calls
    return 0


def decompile_pyc(
    pyc_file: Path,
    *,
    mode: Mode = Mode.HYBRID,
    model: ModelType | None = None,
) -> DecompileReport:
    """Decompile a single .pyc to source according to *mode*."""
    disassembled_pyc, version_tuple, native_code = disassemble_pyc_file(pyc_file)

    if mode == Mode.LLM_ONLY:
        if model is None:
            raise ValueError("llm-only mode requires a model")
        text = decompile_disassembled_pyc(disassembled_pyc, version_tuple, model)
        return DecompileReport(
            source=text,
            mode=mode,
            rule_confidence=None,
            unknown_blocks=0,
            llm_calls=1,
            version_tuple=version_tuple,
        )

    if native_code is None or not rules.supported_version(version_tuple):
        logging.info(
            "Rule engine unavailable for this Python version "
            f"({version_tuple[:2]}); falling back to LLM-only."
        )
        if mode == Mode.RULES_ONLY:
            raise RuntimeError(
                f"Rules-only requested but Python {version_tuple[:2]} is unsupported."
            )
        if model is None:
            raise ValueError("hybrid fallback to LLM requires a model")
        text = decompile_disassembled_pyc(disassembled_pyc, version_tuple, model)
        return DecompileReport(
            source=text,
            mode=Mode.LLM_ONLY,
            rule_confidence=None,
            unknown_blocks=0,
            llm_calls=1,
            version_tuple=version_tuple,
        )

    rule_result = rules.extract_module(native_code)
    unknowns = rule_result.module.unknown_blocks()
    llm_calls = 0
    if mode == Mode.HYBRID and unknowns:
        if model is None:
            raise ValueError("hybrid mode requires a model when unknown blocks remain")
        llm_calls = _fill_module_with_llm(rule_result.module, version_tuple, model)

    return DecompileReport(
        source=rule_result.module.render(),
        mode=mode,
        rule_confidence=rule_result.confidence,
        unknown_blocks=len(unknowns),
        llm_calls=llm_calls,
        version_tuple=version_tuple,
    )


def decompile(
    to_decompile: Path,
    output_path: Path | None,
    model: ModelType | None,
    *,
    mode: Mode = Mode.HYBRID,
) -> None:
    """Backward-compatible entry point.

    - ``to_decompile`` may be a ``.pyc``, ``.py``, or a directory.
    - ``.py`` files are compiled to a temporary ``.pyc`` first.
    - For directories, ``output_path`` (or ``<dir>.decompiled`` if None)
      receives one ``.py`` per ``.pyc`` found.
    """
    if to_decompile.is_dir():
        if output_path is None:
            output_path = to_decompile.parent / (to_decompile.name + ".decompiled")
        _decompile_tree(to_decompile, output_path, model=model, mode=mode)
        return

    pyc_file = _ensure_pyc(to_decompile)
    report = decompile_pyc(pyc_file, mode=mode, model=model)
    logging.info(
        f"Decompiled with mode={report.mode.value}, "
        f"confidence={report.rule_confidence}, "
        f"unknown_blocks={report.unknown_blocks}, "
        f"llm_calls={report.llm_calls}"
    )
    if not output_path:
        logging.info("No output path specified. Printing to stdout...")
        print(report.source)
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report.source)
    logging.info(f"Decompiled Python source code written to: {output_path}")


def _ensure_pyc(path: Path) -> Path:
    if path.suffix == ".pyc":
        return path
    if path.suffix == ".py":
        import py_compile
        import tempfile

        tmp_dir = Path(tempfile.mkdtemp(prefix="pychd-"))
        pyc = tmp_dir / (path.stem + ".pyc")
        py_compile.compile(str(path), cfile=str(pyc), doraise=True)
        return pyc
    raise ValueError(f"Unsupported input file: {path}")


def _iter_input_pycs(root: Path) -> list[Path]:
    return sorted(root.rglob("*.pyc"))


def _decompile_tree(
    src_dir: Path,
    out_dir: Path,
    *,
    model: ModelType | None,
    mode: Mode,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    pycs = _iter_input_pycs(src_dir)
    base = src_dir
    if not pycs:
        py_files = sorted(src_dir.rglob("*.py"))
        if not py_files:
            logging.warning(f"No .pyc or .py files found under {src_dir}")
            return
        import py_compile
        import tempfile

        tmp_root = Path(tempfile.mkdtemp(prefix="pychd-tree-"))
        for py in py_files:
            rel = py.relative_to(src_dir)
            pyc_target = tmp_root / rel.with_suffix(".pyc")
            pyc_target.parent.mkdir(parents=True, exist_ok=True)
            try:
                py_compile.compile(str(py), cfile=str(pyc_target), doraise=True)
                pycs.append(pyc_target)
            except Exception as e:
                logging.warning(f"Skipping {py}: {e}")
        base = tmp_root

    for pyc in pycs:
        try:
            report = decompile_pyc(pyc, mode=mode, model=model)
        except Exception as e:
            logging.error(f"Failed to decompile {pyc}: {e}")
            continue
        rel_name = _output_name(pyc, base=base)
        out_path = out_dir / rel_name
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report.source)
        logging.info(
            f"{pyc} -> {out_path} (mode={report.mode.value}, llm={report.llm_calls})"
        )


def _output_name(pyc: Path, *, base: Path) -> Path:
    try:
        rel = pyc.relative_to(base)
    except ValueError:
        rel = Path(pyc.name)
    name = rel.name
    stem = name.rsplit(".pyc", 1)[0]
    if ".cpython-" in stem:
        stem = stem.split(".cpython-")[0]
    elif ".pypy-" in stem:
        stem = stem.split(".pypy-")[0]
    cleaned_parts = [p for p in rel.parent.parts if p != "__pycache__"]
    return Path(*cleaned_parts, stem + ".py")
