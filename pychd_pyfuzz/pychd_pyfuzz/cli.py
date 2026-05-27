"""``pychd-pyfuzz`` command-line entry point.

Subcommands:

* ``pychd-pyfuzz generate`` — write N samples to a directory, one
  ``.py`` per sample, each accompanied by a ``.tags.json`` sidecar.
* ``pychd-pyfuzz emit`` — print a single sample to stdout (no
  sidecar). Handy for ad-hoc inspection.

The CLI is deliberately thin; library users should call
:class:`pychd_pyfuzz.Fuzzer` directly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .generator import Fuzzer, Sample


def _parse_version(s: str) -> tuple[int, int]:
    parts = s.split(".")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"--target must be MAJOR.MINOR (got {s!r})")
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _write_sample(sample: Sample, out_dir: Path, stem: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    py_path = out_dir / f"{stem}.py"
    tags_path = out_dir / f"{stem}.tags.json"
    py_path.write_text(sample.source)
    tags_path.write_text(
        json.dumps(
            {
                "target": f"{sample.target[0]}.{sample.target[1]}",
                "seed": sample.seed,
                "index": sample.index,
                "length": sample.length,
                "tags": sample.tags,
            },
            indent=2,
        )
    )


def _cmd_generate(args: argparse.Namespace) -> int:
    fuzzer = Fuzzer(
        target=args.target,
        seed=args.seed,
        max_depth=args.max_depth,
        max_top_items=args.max_top_items,
    )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for sample in fuzzer.generate_batch(args.count):
        stem = f"sample_{sample.index:04d}"
        _write_sample(sample, out_dir, stem)
        written += 1
    print(
        f"pychd-pyfuzz: wrote {written} samples (target={args.target[0]}."
        f"{args.target[1]}, seed={args.seed}) to {out_dir}",
        file=sys.stderr,
    )
    return 0


def _cmd_emit(args: argparse.Namespace) -> int:
    fuzzer = Fuzzer(
        target=args.target,
        seed=args.seed,
        max_depth=args.max_depth,
        max_top_items=args.max_top_items,
    )
    sample = fuzzer.generate(index=0)
    sys.stdout.write(sample.source)
    if not sample.source.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pychd-pyfuzz", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser(
        "generate",
        help="write N samples to OUT_DIR as ``sample_NNNN.py`` + ``.tags.json``",
    )
    gen.add_argument("--target", type=_parse_version, default=(3, 14))
    gen.add_argument("--count", type=int, default=10)
    gen.add_argument("--seed", type=int, default=0)
    gen.add_argument("--max-depth", type=int, default=3)
    gen.add_argument("--max-top-items", type=int, default=6)
    gen.add_argument("--out", type=Path, required=True)
    gen.set_defaults(func=_cmd_generate)

    emit = sub.add_parser("emit", help="print a single sample to stdout")
    emit.add_argument("--target", type=_parse_version, default=(3, 14))
    emit.add_argument("--seed", type=int, default=0)
    emit.add_argument("--max-depth", type=int, default=3)
    emit.add_argument("--max-top-items", type=int, default=6)
    emit.set_defaults(func=_cmd_emit)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
