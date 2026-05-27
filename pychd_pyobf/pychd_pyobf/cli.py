"""``pychd-pyobf`` command-line entry point.

Usage::

    pychd-pyobf rewrite IN.pyc OUT.pyc [--mapping mapping.json]
                        [--force-subprocess]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .dispatch import obfuscate


def _cmd_rewrite(args: argparse.Namespace) -> int:
    report = obfuscate(
        args.in_pyc,
        args.out_pyc,
        force_subprocess=args.force_subprocess,
    )
    path = "native" if report.used_native else "subprocess"
    print(
        f"pychd-pyobf: wrote {report.out_path} "
        f"(writer Py {report.version.version[0]}.{report.version.version[1]}, "
        f"{path} path, {report.total_renames()} renames)",
        file=sys.stderr,
    )
    if args.mapping is not None:
        args.mapping.write_text(json.dumps(report.mapping.to_dict(), indent=2))
        print(f"pychd-pyobf: mapping → {args.mapping}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pychd-pyobf", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    rew = sub.add_parser("rewrite", help="anonymise IN.pyc → OUT.pyc")
    rew.add_argument("in_pyc", type=Path)
    rew.add_argument("out_pyc", type=Path)
    rew.add_argument(
        "--mapping",
        type=Path,
        default=None,
        help="optional path to dump the original→anonymised JSON map",
    )
    rew.add_argument(
        "--force-subprocess",
        action="store_true",
        help=(
            "always take the subprocess path even when the writer minor"
            " matches the current interpreter (useful for testing the"
            " cross-version code)"
        ),
    )
    rew.set_defaults(func=_cmd_rewrite)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
