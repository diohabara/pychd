import dis
import io
import logging
import marshal
import sys
import textwrap
from pathlib import Path

from litellm import completion
from xdis.magics import magic_int2tuple

from pychd.types import ModelType


def disassemble_pyc_file(pyc_file: Path) -> str:
    with open(pyc_file, "rb") as f:
        skip_bytes = 16  # TODO: This is a hack. Fix this.
        # Read the first 4 bytes, which contain the magic number
        magic_word = f.read(4)
        skip_bytes -= 4
        logging.debug(f"{magic_word=}")
        magic_int = int.from_bytes(magic_word[:2], "little")
        logging.debug(f"{magic_int=}")
        (pyc_major_version, pyc_minor_version, pyc_micro_version) = magic_int2tuple(
            magic_int
        )
        logging.debug(
            f"{pyc_major_version=}, {pyc_minor_version=}, {pyc_micro_version=}"
        )
        py_major_version, py_minor_version, _, _, _ = sys.version_info
        if not (
            pyc_major_version == py_major_version
            and pyc_minor_version == py_minor_version
        ):
            logging.error(
                f"Python bytecode ({pyc_major_version}.{pyc_minor_version}) \
                    and your Python version ({py_major_version}.{py_minor_version}.) \
                        are incompatible"
            )
            sys.exit(1)
        logging.debug(
            f"Python bytecode uses Python {py_major_version}.{py_minor_version}"
        )
        f.read(skip_bytes)
        bytecode = marshal.load(f)
        logging.debug(f"{bytecode=}")
    original_stdout = sys.stdout
    string_output = io.StringIO()
    sys.stdout = string_output
    dis.dis(bytecode)
    sys.stdout = original_stdout
    disassembled_pyc = string_output.getvalue()

    logging.info(
        textwrap.dedent(
            f"""\
              ⭐⭐⭐ Disassembled Python bytecode STARTS ⭐⭐⭐
              {disassembled_pyc}
              ⭐⭐⭐ Disassembled Python bytecode ENDS ⭐⭐⭐
      """
        )
    )
    return str(disassembled_pyc)


def decompile_disassembled_pyc(disassembled_pyc: str, model: ModelType) -> str:
    logging.info(f"{model=}")
    user_prompt = textwrap.dedent(
        f"""\
        You are a Python decompiler.
        You will be given a disassembled Python bytecode.
        Decompile it into the original source code.
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
    disassembled_pyc = disassemble_pyc_file(input_pyc_file)
    logging.info("Decompiling disassembled Python bytecode...")
    decompiled_py = decompile_disassembled_pyc(disassembled_pyc, model)
    # if no path is specified, print to stdout
    if not output_path:
        logging.info("No output path specified. Printing to stdout...")
        print(decompiled_py)
        return
    # if path is specified, write to file
    with open(output_path, "w") as f:
        f.write(decompiled_py)
    logging.info(f"Decompiled Python source code written to: {output_path}")
