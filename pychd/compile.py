import argparse
import compileall
import logging
import py_compile
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compile Python source files into bytecode.",
        epilog="Example: python generate_bytecode.py",
    )
    parser.add_argument("directory", help="Directory to compile", type=str)
    return parser.parse_args()


def compile(to_compile: Path, output_path: Path | None) -> None:
    if output_path is None:
        output_path = to_compile.with_suffix(".pyc")
    if to_compile.is_dir():
        logging.info(f"Compiling Python source files... {to_compile}")
        compileall.compile_dir(to_compile, ddir=output_path)
    else:
        logging.info(f"Compiling Python source files... {to_compile}")
        py_compile.compile(str(to_compile), cfile=str(output_path))
