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
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import CodeType
from typing import Any

import litellm
from litellm import completion, token_counter
from xdis.disasm import disco
from xdis.load import load_module

from pychd import cross_version, ir, rules
from pychd.types import ModelType


class Mode(str, Enum):
    HYBRID = "hybrid"
    RULES_ONLY = "rules-only"
    LLM_ONLY = "llm-only"
    # ``HYBRID_REWRITE`` — runs the rule pass for declaration scaffolding,
    # then asks the LLM to rewrite the whole module given (disassembly +
    # rule output) as context. The strongest mode for ``strict_match`` /
    # ``FC`` because the LLM both fills function bodies *and* corrects
    # module-level statements the rule walker mis-recovered (for-loop
    # leaks, comprehensions, complex RHS, etc.). Costs one LLM call per
    # module instead of one per body, which actually keeps wallclock
    # comparable to per-body ``HYBRID`` on modules with many small
    # functions.
    HYBRID_REWRITE = "hybrid-rewrite"


class Backend(str, Enum):
    """Which inference backend the hybrid / llm-only paths should use.

    Two backends are shipped:

    * ``LITELLM`` — the historical default. Routes via the
      :mod:`litellm` Python SDK; works against any provider litellm
      supports (OpenAI, Anthropic, Google, local Ollama, …) and reads
      credentials from the standard environment variables.
    * ``CODEX`` — invokes the OpenAI Codex CLI (``codex exec``) as a
      subprocess. The CLI handles its own auth via the user's prior
      ``codex login``, runs without an explicit API key on this host,
      and inherits whichever model the user has configured in
      ``~/.codex/config.toml``.

    The split exists so reviewers who only have a Codex login (no
    OpenAI/Anthropic API key) can still drive the hybrid body-fill
    path end-to-end.
    """

    LITELLM = "litellm"
    CODEX = "codex"


@dataclass
class DecompileReport:
    source: str
    mode: Mode
    rule_confidence: float | None
    unknown_blocks: int
    llm_calls: int
    version_tuple: tuple


def _disassemble_native(pyc_file: Path) -> tuple[str, CodeType]:
    """Disassemble using the standard library (current interpreter only).

    Uses :func:`dis.dis`'s explicit ``file=`` parameter rather than
    ``contextlib.redirect_stdout`` — the redirect approach mutates the
    process-global ``sys.stdout`` and is not thread-safe, which caused
    the disassembled text of one module to leak into the benchmark's
    JSON output whenever two threads disassembled concurrently
    (`--parallel >1`).
    """
    with open(pyc_file, "rb") as f:
        f.read(16)  # 16-byte .pyc header in 3.7+
        bytecode = marshal.load(f)
    string_output = io.StringIO()
    dis.dis(bytecode, file=string_output)
    return string_output.getvalue(), bytecode


def disassemble_pyc_file(
    pyc_file: Path,
) -> tuple[str, tuple, CodeType | None, Any | None]:
    """Disassemble a .pyc file from any Python version.

    Returns ``(disassembled_text, version_tuple, native_code, xdis_code)``.

    - ``native_code`` is the stdlib :class:`types.CodeType` when the
      .pyc matches the running interpreter (which lets the *native*
      rule pass run on it); otherwise ``None``.
    - ``xdis_code`` is always the :mod:`xdis` code object returned by
      :func:`xdis.load.load_module`; the *cross-version* rule pass
      walks this for non-current versions.
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
    return disassembled_pyc, version_tuple, native_code, co


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


_BODY_FILL_PROMPT = textwrap.dedent(
    """\
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


# Whole-module rewrite prompt used by :data:`Mode.HYBRID_REWRITE`. The
# LLM gets both the disassembly (authoritative) and the rule pass'
# partial recovery (declarations are reliable, bodies and module-level
# control flow may be wrong) and is expected to emit a complete,
# directly-compilable Python source file.
_MODULE_REWRITE_PROMPT = textwrap.dedent(
    """\
    You are a Python decompiler. Reconstruct the original Python
    {version_str} source for an entire module from its disassembled
    bytecode.

    You are given two inputs:

    1. The complete disassembled bytecode (authoritative — your
       output must compile back to behaviorally-equivalent bytecode).
    2. A partial rule-based recovery (declarations are reliable, but
       function bodies and some module-level statements may be
       missing, stubbed as ``pass  # pychd: unrecovered body``, or
       mis-recovered as ``X = ...``).

    Bytecode disassembly:
    ```
    {disassembly}
    ```

    Partial recovery (use as a structural hint, but fix any mistakes
    you can see from the bytecode):
    ```
    {rule_output}
    ```

    Requirements for your output:

    - Output ONLY valid Python {version_str} source code. No
      explanations, no markdown fences, no leading/trailing prose.
    - Preserve every class / function / method / import / decorator /
      argument name from the partial recovery exactly.
    - Reconstruct function bodies from the disassembly. Use the rule
      pass' signatures verbatim — do not invent new parameters,
      defaults, or decorators.
    - Fix module-level statements (assignments, for-loops,
      comprehensions, ``if __name__ == "__main__":`` guards, etc.)
      that the rule pass got wrong by reading the bytecode.
    - Do NOT add any imports the bytecode does not justify.
    - The output must pass ``ast.parse`` and ``py_compile.compile``
      under Python {version_str}.
    """
)


def _llm_fill_body(
    signature: str,
    disassembly: str,
    version_tuple: tuple,
    model: ModelType | None,
    backend: "Backend | None" = None,
) -> str:
    """Ask the configured LLM backend to reconstruct one function body.

    The litellm path is the historical default — every provider with a
    litellm adapter (OpenAI, Anthropic, Google, …) routes through it.
    The codex path shells out to ``codex exec`` so reviewers with a
    Codex login but no direct API key can still run the hybrid
    body-fill end-to-end.
    """
    version_str = f"{version_tuple[0]}.{version_tuple[1]}"
    user_prompt = _BODY_FILL_PROMPT.format(
        version_str=version_str,
        signature=signature,
        disassembly=disassembly,
    )
    if backend is None:
        backend = Backend.LITELLM
    if backend == Backend.CODEX:
        text = _codex_fill_body(user_prompt, model)
    else:
        response = completion(  # pyrefly: ignore[not-callable]
            model=model,
            temperature=0.2,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = response.choices[0].message.content
    return _clean_llm_body(text, signature)


# Codex defaults for the body-fill prompt. Reviewers wanted the
# strongest model the Codex CLI exposes — ``gpt-5.5`` with extra-high
# reasoning effort. The latter is set via the per-call ``-c
# model_reasoning_effort='"xhigh"'`` override so the Codex CLI's own
# ``~/.codex/config.toml`` stays unmodified. The model can still be
# overridden by passing an explicit ``model`` argument (or by
# configuring a Codex CLI profile).
_CODEX_DEFAULT_MODEL = "gpt-5.5"
_CODEX_DEFAULT_REASONING = "xhigh"


def _codex_fill_body(prompt: str, model: ModelType | None) -> str:
    """Invoke the Codex CLI as a subprocess and capture its response.

    Codex emits status / event lines on stdout and writes the agent's
    final message to the file given via ``-o``. We use ``--ephemeral``
    + ``--skip-git-repo-check`` so each call is stateless and works
    from any host directory. The sandbox is set to ``read-only`` —
    body-fill should never need to run commands, write files, or
    touch the network beyond the model call itself.

    When no *model* is given we fall back to :data:`_CODEX_DEFAULT_MODEL`
    + :data:`_CODEX_DEFAULT_REASONING` — currently ``gpt-5.5`` with
    extra-high reasoning effort, which the user picked as the strongest
    body-recovery configuration available through the Codex CLI's
    ChatGPT account auth path.
    """
    binary = shutil.which("codex")
    if binary is None:
        raise RuntimeError(
            "codex CLI not found in PATH; install it from "
            "https://github.com/openai/codex"
        )
    with tempfile.NamedTemporaryFile(
        suffix=".txt", mode="w+", delete=False
    ) as out_file:
        out_path = out_file.name
    try:
        cmd = [
            binary,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "-o",
            out_path,
            # Pin the strongest reasoning tier; overridden if the
            # caller's Codex profile already requests another effort.
            "-c",
            f'model_reasoning_effort="{_CODEX_DEFAULT_REASONING}"',
        ]
        model_to_use = str(model) if model is not None else _CODEX_DEFAULT_MODEL
        cmd.extend(["-m", model_to_use])
        # Large modules' disassemblies blow past ARG_MAX (~128 KB on
        # Linux) when passed as a command-line argument. Use the
        # documented ``-`` placeholder which makes codex exec read the
        # prompt from stdin instead. The body-fill prompts stay well
        # under the limit but the whole-module rewrite prompts
        # (HYBRID_REWRITE) routinely cross it.
        cmd.append("-")
        # Large stdlib modules (argparse, typing, _pydecimal) push the
        # ``xhigh`` reasoning effort past 4 minutes per module. 15 min
        # accommodates them without sacrificing the failure signal for
        # genuinely stuck calls. The wall-clock impact is bounded by
        # the harness' thread-pool size, not this per-call cap.
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            check=False,
            timeout=900,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"codex exec exited {proc.returncode}: {proc.stderr.strip()[:500]}"
            )
        try:
            with open(out_path, encoding="utf-8") as fh:
                return fh.read()
        except OSError as e:
            raise RuntimeError(f"codex output file missing: {e}") from e
    finally:
        try:
            Path(out_path).unlink(missing_ok=True)
        except OSError:
            pass


def _clean_llm_body(text: str, signature: str) -> str:
    # Trim only trailing whitespace; the *leading* indentation has
    # to survive intact so :func:`_reindent_to_four_spaces` can do
    # its uniform-dedent. A naive ``str.strip`` here would knock the
    # first line down to column 0 and force the rest of the body to
    # appear over-indented relative to it.
    text = text.rstrip()
    # If the model wrapped the output in a triple-backtick fence
    # (most are explicitly told not to, but some still do) the
    # opening ``` is at column 0; strip those *lines* without
    # touching body indentation.
    if text.lstrip().startswith("```"):
        lines = text.splitlines()
        while lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        while lines and lines[-1].lstrip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    sig_prefix = signature.split("(")[0].strip()
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        if sig_prefix and line.strip().startswith(sig_prefix):
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines).rstrip()
    # Strip a leading docstring if the LLM re-emitted one — the rule
    # pass already captured the original from ``co_consts[0]``, so
    # keeping the LLM's duplicate would produce ``def foo(): \n
    # """docstring""" \n """docstring""" \n body``.
    cleaned = _strip_leading_docstring(cleaned)
    # Normalise to a single 4-space indent: strong models often
    # emit the body with the surrounding ``def`` block's indentation
    # (8 spaces deep when inside a class). The rule-pass renderer
    # then concatenates that with its own 4-space indent and the
    # result fails ``ast.parse`` with an "unexpected indent".
    cleaned = _reindent_to_four_spaces(cleaned)
    return cleaned


def _reindent_to_four_spaces(body: str) -> str:
    """Strip every line back to the function-body root indent.

    The :class:`pychd.ir.RawStatement` renderer adds the surrounding
    function's own indent on top of whatever the body text contains.
    So *body* should be returned with **no** leading indentation —
    just the dedented statement sequence. Strong models often emit
    the body with the enclosing ``def`` block's indentation already
    baked in (4 – 8 spaces); we run :func:`textwrap.dedent` to peel
    those off uniformly so the final output has exactly one indent
    level (the renderer's own).
    """
    if not body.strip():
        return body
    expanded = "\n".join(line.expandtabs(4) for line in body.splitlines())
    dedented = textwrap.dedent(expanded)
    return dedented.rstrip()


def _strip_leading_docstring(body: str) -> str:
    """Drop a leading triple-quoted string from *body* (with its
    surrounding indent) if present. Whitespace-only lines before the
    docstring are preserved so the remaining body's indentation is
    untouched."""
    lines = body.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return body
    stripped = lines[i].lstrip()
    for quote in ('"""', "'''"):
        if not stripped.startswith(quote):
            continue
        # Single-line docstring: ``"""hello"""``.
        rest = stripped[len(quote) :]
        if quote in rest:
            return "\n".join(lines[i + 1 :])
        # Multi-line — find the closing quote.
        for j in range(i + 1, len(lines)):
            if quote in lines[j]:
                return "\n".join(lines[j + 1 :])
        return body
    return body


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
    model: ModelType | None,
    backend: Backend = Backend.LITELLM,
) -> int:
    calls = 0
    for stmt in module.body:
        calls += _fill_stmt_with_llm(stmt, version_tuple, model, backend)
    return calls


def _fill_stmt_with_llm(
    stmt: ir.Stmt,
    version_tuple: tuple,
    model: ModelType | None,
    backend: Backend = Backend.LITELLM,
) -> int:
    if isinstance(stmt, (ir.FunctionDef, ir.ClassDef)):
        calls = 0
        new_body: list[ir.Stmt] = []
        for inner in stmt.body:
            if isinstance(inner, ir.UnknownBlock):
                sig = inner.signature or (
                    f"<{type(stmt).__name__}.{getattr(stmt, 'name', '?')}>"
                )
                body_src = _llm_fill_body(
                    sig, inner.disassembly, version_tuple, model, backend
                )
                if not body_src.strip():
                    body_src = "pass"
                new_body.append(ir.RawStatement(source=body_src))
                calls += 1
            elif isinstance(inner, (ir.FunctionDef, ir.ClassDef)):
                calls += _fill_stmt_with_llm(inner, version_tuple, model, backend)
                new_body.append(inner)
            else:
                new_body.append(inner)
        stmt.body = new_body
        return calls
    return 0


def _llm_rewrite_module(
    disassembly: str,
    rule_output: str,
    version_tuple: tuple,
    model: ModelType | None,
    backend: Backend = Backend.LITELLM,
) -> str:
    """Send (disassembly + rule output) to the LLM and get back a full
    recovered Python module source.

    Used by :data:`Mode.HYBRID_REWRITE`. The prompt instructs the LLM
    to preserve every identifier from the rule output verbatim while
    using the disassembly to fix bodies and any mis-recovered
    module-level statements. One call per module — much cheaper than
    per-body filling for modules with many small functions, and the
    LLM can also fix module-level shape (for-loop leaks, comprehensions,
    complex literal RHS) that per-body filling cannot reach.
    """
    version_str = f"{version_tuple[0]}.{version_tuple[1]}"
    prompt = _MODULE_REWRITE_PROMPT.format(
        version_str=version_str,
        disassembly=disassembly,
        rule_output=rule_output,
    )
    if backend == Backend.CODEX:
        text = _codex_fill_body(prompt, model)
    else:
        if model is None:
            raise ValueError("llm rewrite requires a model when using litellm backend")
        response = completion(  # pyrefly: ignore[not-callable]
            model=model,
            temperature=0.1,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.choices[0].message.content
    return _clean_module_rewrite(text)


def _clean_module_rewrite(text: str) -> str:
    """Strip surrounding fences / stray prose from an LLM whole-module
    rewrite. Keeps body indentation intact (unlike the per-body cleaner,
    which dedents to four spaces)."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop opening fence (``` or ```python).
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        # Drop closing fence if present.
        while lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.rstrip() + "\n"


def decompile_pyc(
    pyc_file: Path,
    *,
    mode: Mode = Mode.HYBRID,
    model: ModelType | None = None,
    backend: Backend = Backend.LITELLM,
) -> DecompileReport:
    """Decompile a single .pyc to source according to *mode*.

    Dispatch:

    - **llm-only** — always feeds the full xdis disassembly to the LLM.
    - **rules-only / hybrid** — chooses the highest-fidelity rule pass
      available for the bytecode's version:

      * 3.14 .pyc on a 3.14 interpreter → :mod:`pychd.rules` native pass.
      * any other CPython 3.x → :mod:`pychd.cross_version` xdis pass.
      * neither available → fall through to LLM-only (or raise in
        rules-only mode).
    """
    disassembled_pyc, version_tuple, native_code, xdis_code = disassemble_pyc_file(
        pyc_file
    )

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

    # Choose the most accurate rule pass available.
    rule_result = _run_rule_pass(version_tuple, native_code, xdis_code)
    if rule_result is None:
        logging.info(
            "No rule pass available for Python "
            f"{version_tuple[:2]}; falling back to LLM-only."
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

    module, confidence = rule_result
    unknowns = module.unknown_blocks()
    llm_calls = 0
    if mode == Mode.HYBRID and unknowns:
        # The codex backend handles auth via ``codex login`` rather
        # than an OPENAI_API_KEY env var, so the ``model`` argument is
        # optional for that path (defaults to the model the user has
        # configured in ``~/.codex/config.toml``).
        if model is None and backend != Backend.CODEX:
            raise ValueError("hybrid mode requires a model when unknown blocks remain")
        llm_calls = _fill_module_with_llm(module, version_tuple, model, backend)

    if mode == Mode.HYBRID_REWRITE:
        if model is None and backend != Backend.CODEX:
            raise ValueError("hybrid-rewrite mode requires a model with the litellm backend")
        rule_source = module.render()
        # If the rule pass already left no unknown bodies *and* the rule
        # output parses cleanly, skip the LLM call. Saves both wall-clock
        # and codex tokens for shapes the rule pass nailed (re-export
        # modules, trivial-body modules).
        skip_llm = False
        if not unknowns:
            import ast as _ast

            try:
                _ast.parse(rule_source)
                skip_llm = True
            except SyntaxError:
                pass
        if skip_llm:
            return DecompileReport(
                source=rule_source,
                mode=mode,
                rule_confidence=confidence,
                unknown_blocks=0,
                llm_calls=0,
                version_tuple=version_tuple,
            )
        try:
            rewritten = _llm_rewrite_module(
                disassembled_pyc, rule_source, version_tuple, model, backend
            )
        except Exception as e:  # noqa: BLE001
            # Any LLM failure (timeout, transport, bad output) falls back
            # to the rule-only source. The caller cares about source
            # quality, not why the rewrite failed — but we log so the
            # benchmark row can show the degradation.
            logging.warning(
                "LLM rewrite of %s failed (%s); falling back to rule-only output.",
                pyc_file,
                str(e)[:120],
            )
            return DecompileReport(
                source=rule_source,
                mode=mode,
                rule_confidence=confidence,
                unknown_blocks=len(unknowns),
                llm_calls=0,
                version_tuple=version_tuple,
            )
        import ast as _ast

        try:
            _ast.parse(rewritten)
            source = rewritten
            llm_calls += 1
        except SyntaxError:
            logging.warning(
                "LLM rewrite of %s did not parse; falling back to rule-only output.",
                pyc_file,
            )
            source = rule_source
        return DecompileReport(
            source=source,
            mode=mode,
            rule_confidence=confidence,
            unknown_blocks=len(unknowns),
            llm_calls=llm_calls,
            version_tuple=version_tuple,
        )

    return DecompileReport(
        source=module.render(),
        mode=mode,
        rule_confidence=confidence,
        unknown_blocks=len(unknowns),
        llm_calls=llm_calls,
        version_tuple=version_tuple,
    )


def _run_rule_pass(
    version_tuple: tuple,
    native_code: CodeType | None,
    xdis_code: Any | None,
) -> tuple[ir.Module, float] | None:
    """Pick the best available rule pass for this bytecode version.

    Returns ``(module, confidence)`` or ``None`` when no pass applies.

    Failures inside the cross-version walker (unknown opcode, malformed
    xdis instruction, …) are logged with a full traceback at WARNING
    and surfaced as ``None`` — the caller decides whether to fall back
    to LLM-only (hybrid) or raise (rules-only). Swallowing the
    exception silently here would mask real regressions in the
    cross-version walker; logging keeps debugging tractable.
    """
    if native_code is not None and rules.native_supported(version_tuple):
        result = rules.extract_module(native_code)
        return result.module, result.confidence
    if xdis_code is not None and cross_version.supports(version_tuple):
        try:
            result_cv = cross_version.extract_module(xdis_code, version_tuple)
        except Exception:
            logging.warning(
                "cross-version rule pass crashed for Python %s; falling "
                "back to LLM-only path. Re-run with --verbose for the "
                "full traceback.",
                version_tuple[:2],
                exc_info=True,
            )
            return None
        return result_cv.module, result_cv.confidence
    return None


def decompile(
    to_decompile: Path,
    output_path: Path | None,
    model: ModelType | None,
    *,
    mode: Mode = Mode.HYBRID,
    backend: Backend = Backend.LITELLM,
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
        _decompile_tree(
            to_decompile, output_path, model=model, mode=mode, backend=backend
        )
        return

    pyc_file = _ensure_pyc(to_decompile)
    report = decompile_pyc(pyc_file, mode=mode, model=model, backend=backend)
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
    backend: Backend = Backend.LITELLM,
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
            report = decompile_pyc(pyc, mode=mode, model=model, backend=backend)
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
