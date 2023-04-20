import dis
import io
import logging
import marshal
import os
import sys
import textwrap
from pathlib import Path

import openai


def disassemble_pyc_file(pyc_file: Path) -> str:
    with open(pyc_file, 'rb') as f:
        # Read the first 16 bytes, which contain the magic number, timestamp, and size
        _header = f.read(16)
        # Read the remaining bytecode
        try:
            bytecode = marshal.load(f)
        except ValueError:
            print('Python bytecode uses and your Python version are incompatible')
            sys.exit(1)

    # Save the original standard output
    original_stdout = sys.stdout

    # Redirect standard output to a StringIO object
    string_output = io.StringIO()
    sys.stdout = string_output

    # Run dis.dis and capture the output
    dis.dis(bytecode)

    # Reset standard output to the original
    sys.stdout = original_stdout

    # Get the output as a string
    disassembled_pyc = string_output.getvalue()

    logging.info(f"Disassembled Python bytecode: {disassembled_pyc}")
    return str(disassembled_pyc)

def decompile_disassembled_pyc(disassembled_pyc: str) -> str:
    model = "gpt-3.5-turbo"
    temperature=0.7
    user_prompt=textwrap.dedent(f"""\
        You are a Python decompiler.
        You will be given a disassembled Python bytecode.
        You must decompile it into the original source code.
        You only output the original full source code, not the natural language description.
        ```
        {disassembled_pyc}
        ```
        """
    )
    response = openai.ChatCompletion.create(
        model=model,
        temperature=temperature,
        messages=[{
            "role": "user",
            "content": user_prompt
        }],
    )
    logging.info(response)
    generated_text = response.choices[0].message.content
    return generated_text

def setup() -> None:
    logging.basicConfig(level=logging.INFO)
    openai.api_key = os.getenv("OPENAI_API_KEY")

def main() -> None:
    setup()
    if len(sys.argv) != 2:
        print('Usage: python disassemble_pyc.py <path_to_pyc_file>')
    else:
        logging.info('Disassembling Python bytecode file...')
        input_pyc_file = Path(sys.argv[1])
        logging.info(f'Input Python bytecode file: {input_pyc_file}')
        disassembled_pyc = disassemble_pyc_file(input_pyc_file)
        logging.info('Decompiling disassembled Python bytecode...')
        decompiled_py = decompile_disassembled_pyc(disassembled_pyc)
        print(decompiled_py)

if __name__ == '__main__':
    main()

