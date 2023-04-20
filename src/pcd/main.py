import argparse
import logging
from logging.config import fileConfig
from pathlib import Path

from . import compile, decompile


def parse_args() -> argparse.Namespace:
    # create the top-level parser
    parser = argparse.ArgumentParser(
        description="Decompile|Compile Python source files into bytecode."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # create the parser for the "decompile" command
    parser_decompile = subparsers.add_parser(
        "decompile", help="Decompile Python source files into bytecode."
    )
    parser_decompile.add_argument("path", help="Path to decompile", type=str)
    parser_decompile.add_argument(
        "-o", "--output", help="Output path", type=str, required=False
    )

    # create the parser for the "compile" command
    parser_compile = subparsers.add_parser(
        "compile", help="Compile Python source files into bytecode."
    )
    parser_compile.add_argument("path", help="Path to compile", type=str)

    return parser.parse_args()


def setup() -> None:
    logging_config = Path(__file__).parent / "logging.conf"
    fileConfig(logging_config)


def cli() -> None:
    setup()
    args = parse_args()
    logging.info(args)
    if args.command == "compile":
        to_compile = Path(args.path)
        compile.compile(to_compile=to_compile)
    elif args.command == "decompile":
        to_decompile = Path(args.path)
        output_path = Path(args.output) if args.output else None
        decompile.decompile(to_decompile=to_decompile, output_path=output_path)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
