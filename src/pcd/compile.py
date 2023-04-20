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


def compile(to_compile: Path) -> None:
    if to_compile.is_dir():
        logging.info("Compiling Python source files...")
        compileall.compile_dir(to_compile)
    else:
        logging.info("Compiling Python source file...")
        py_compile.compile(str(to_compile))
