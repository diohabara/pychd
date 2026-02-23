import dis
import io
import logging
import marshal
import sys
import textwrap
from pathlib import Path

from litellm import completion
from xdis.disasm import disco
from xdis.load import load_module

from pychd.types import ModelType


def _disassemble_native(pyc_file: Path) -> str:
    """Disassemble using the standard library (current interpreter version only)."""
    with open(pyc_file, "rb") as f:
        # Python 3.7+ uses a 16-byte header
        f.read(16)
        bytecode = marshal.load(f)
    string_output = io.StringIO()
    original_stdout = sys.stdout
    sys.stdout = string_output
    dis.dis(bytecode)
    sys.stdout = original_stdout
    return string_output.getvalue()


def disassemble_pyc_file(pyc_file: Path) -> tuple[str, tuple]:
    """Disassemble a .pyc file from any Python version.

    Uses xdis for cross-version disassembly. Falls back to the standard
    library when the .pyc matches the current interpreter version and
    xdis encounters an error.

    Returns a tuple of (disassembled_text, version_tuple).
    """
    (version_tuple, timestamp, magic_int, co, is_pypy, source_size, sip_hash) = (
        load_module(str(pyc_file))
    )
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
            disassembled_pyc = _disassemble_native(pyc_file)
        else:
            raise

    logging.info(
        textwrap.dedent(
            f"""\
              ⭐⭐⭐ Disassembled Python bytecode STARTS ⭐⭐⭐
              {disassembled_pyc}
              ⭐⭐⭐ Disassembled Python bytecode ENDS ⭐⭐⭐
      """
        )
    )
    return disassembled_pyc, version_tuple


def decompile_disassembled_pyc(
    disassembled_pyc: str, version_tuple: tuple, model: ModelType
) -> str:
    logging.info(f"{model=}")
    version_str = f"{version_tuple[0]}.{version_tuple[1]}"
    user_prompt = textwrap.dedent(
        f"""\
        You are a Python decompiler.
        You will be given a disassembled Python {version_str} bytecode.
        Decompile it into the original Python {version_str} source code.
        Output only the original full source code.
        Do not the natural language description.
        Do not surround the code with triple quotes such as '```' or '```python'.
        ```
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


def decompile(
    to_decompile: Path,
    output_path: Path | None,
    model: ModelType,
) -> None:
    logging.info("Disassembling Python bytecode file...")
    input_pyc_file = to_decompile
    logging.info(f"Input Python bytecode file: {input_pyc_file}")
    disassembled_pyc, version_tuple = disassemble_pyc_file(input_pyc_file)
    logging.info("Decompiling disassembled Python bytecode...")
    decompiled_py = decompile_disassembled_pyc(disassembled_pyc, version_tuple, model)
    # if no path is specified, print to stdout
    if not output_path:
        logging.info("No output path specified. Printing to stdout...")
        print(decompiled_py)
        return
    # if path is specified, write to file
    with open(output_path, "w") as f:
        f.write(decompiled_py)
    logging.info(f"Decompiled Python source code written to: {output_path}")
