import argparse
import logging
from pathlib import Path

from pychd import compile, decompile
from pychd.decompile import Mode
from pychd.validate import validate, validate_directory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Decompile / compile / validate Python bytecode."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_decompile = subparsers.add_parser(
        "decompile",
        help="Decompile a .pyc file, a .py file (compiled on the fly), "
        "or a directory tree of either.",
    )
    parser_decompile.add_argument("path", help="Path to decompile", type=str)
    parser_decompile.add_argument(
        "-o",
        "--output",
        help="Output path (file or directory)",
        type=str,
        required=False,
    )
    parser_decompile.add_argument(
        "-v", "--verbose", help="Increase output verbosity", action="store_true"
    )
    mode_group = parser_decompile.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--rules-only",
        action="store_true",
        help="Skip the LLM entirely; emit `pass` placeholders for unrecovered bodies.",
    )
    mode_group.add_argument(
        "--llm-only",
        action="store_true",
        help="Bypass the rule engine and send the full disassembly to the LLM.",
    )
    parser_decompile.add_argument(
        "-m",
        "--model",
        help=(
            "Model to use when the LLM is invoked. Examples: "
            "`gpt-4o`, `claude-sonnet-4-5`, `ollama/llama3`. "
            "Required unless --rules-only is set."
        ),
        type=str,
        required=False,
        default="ollama/deepseek-r1",
    )

    parser_compile = subparsers.add_parser(
        "compile", help="Compile a Python source file or directory to .pyc."
    )
    parser_compile.add_argument("path", help="Path to compile", type=str)
    parser_compile.add_argument(
        "-o", "--output", help="Output path", type=str, required=False
    )
    parser_compile.add_argument(
        "-v", "--verbose", help="Increase output verbosity", action="store_true"
    )

    parser_validate = subparsers.add_parser(
        "validate",
        help="AST-compare original source against decompiled output.",
    )
    parser_validate.add_argument(
        "original", help="Original .py file or directory", type=str
    )
    parser_validate.add_argument(
        "decompiled", help="Decompiled .py file or directory", type=str
    )
    parser_validate.add_argument(
        "-v", "--verbose", help="Increase output verbosity", action="store_true"
    )
    parser_validate.add_argument(
        "--ignore-annotations",
        action="store_true",
        help="Strip annotations from both ASTs before comparing.",
    )

    return parser.parse_args()


def setup(args: argparse.Namespace) -> None:
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level)


def _mode_from_args(args: argparse.Namespace) -> Mode:
    if args.rules_only:
        return Mode.RULES_ONLY
    if args.llm_only:
        return Mode.LLM_ONLY
    return Mode.HYBRID


def cli(args: argparse.Namespace) -> None:
    if args.command == "compile":
        to_compile = Path(args.path)
        output_path = Path(args.output) if args.output else None
        compile.compile(to_compile=to_compile, output_path=output_path)
    elif args.command == "decompile":
        to_decompile = Path(args.path)
        output_path = Path(args.output) if args.output else None
        mode = _mode_from_args(args)
        model = None if mode == Mode.RULES_ONLY else args.model
        decompile.decompile(
            to_decompile=to_decompile,
            output_path=output_path,
            model=model,
            mode=mode,
        )
    elif args.command == "validate":
        original = Path(args.original)
        decompiled = Path(args.decompiled)
        ignore_annotations = bool(getattr(args, "ignore_annotations", False))
        if original.is_dir() and decompiled.is_dir():
            results = validate_directory(
                original, decompiled, ignore_annotations=ignore_annotations
            )
            matches = sum(1 for _, r in results if r.match)
            total = len(results)
            for name, result in results:
                status = "MATCH" if result.match else "DIFFER"
                print(f"  {status}: {name} - {result.details}")
            print(f"\nResult: {matches}/{total} files match")
        else:
            result = validate(
                original, decompiled, ignore_annotations=ignore_annotations
            )
            status = "MATCH" if result.match else "DIFFER"
            print(f"{status}: {result.details}")


def main() -> None:
    args = parse_args()
    setup(args)
    cli(args)


if __name__ == "__main__":
    main()
