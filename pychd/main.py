import argparse
import logging
from pathlib import Path

from pychd import compile, decompile


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
    parser_decompile.add_argument(
        "-v", "--verbose", help="Increase output verbosity", action="store_true"
    )
    parser_decompile.add_argument(
        "-m",
        "--model",
        help=(
            "Model to use for decompilation"
            " (e.g. gpt-4o, claude-sonnet-4-20250514,"
            " gemini/gemini-2.0-flash, ollama/llama3)"
        ),
        type=str,
        required=False,
        default="ollama/deepseek-r1",
    )

    # create the parser for the "compile" command
    parser_compile = subparsers.add_parser(
        "compile", help="Compile Python source files into bytecode."
    )
    parser_compile.add_argument("path", help="Path to compile", type=str)
    parser_compile.add_argument(
        "-o", "--output", help="Output path", type=str, required=False
    )
    parser_compile.add_argument(
        "-v", "--verbose", help="Increase output verbosity", action="store_true"
    )

    return parser.parse_args()


def setup(args: argparse.Namespace) -> None:
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)


def cli(args: argparse.Namespace) -> None:
    if args.command == "compile":
        to_compile = Path(args.path)
        output_path = Path(args.output) if args.output else None
        # pyrefly: ignore[missing-attribute]
        compile.compile(to_compile=to_compile, output_path=output_path)
    elif args.command == "decompile":
        to_decompile = Path(args.path)
        output_path = Path(args.output) if args.output else None
        decompile.decompile(
            to_decompile=to_decompile, output_path=output_path, model=args.model
        )


def main() -> None:
    args = parse_args()
    setup(args)
    cli(args)


if __name__ == "__main__":
    main()
