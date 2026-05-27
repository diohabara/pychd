# pychd-pyfuzz

Random, syntactically-valid Python source generator. Built to feed
contamination-free benchmarks for `pychd` (the Python `.pyc`
decompiler) and any other decompiler that wants the same.

Every sample is constructed directly from `ast.AST` nodes (not text
templates), so it is — by construction — a code path that has never
appeared anywhere on the internet and cannot be in any LLM's training
data. Pair with `pychd-pyobf` (which strips identifiers + constants
from compiled `.pyc`) for the strongest available contamination
guarantee.

See the main [pychd README](https://github.com/diohabara/pychd) for
the broader story.

## Install

```bash
pip install pychd-pyfuzz
```

## Use

```bash
# 100 samples targeting Python 3.14:
pychd-pyfuzz generate --target 3.14 --count 100 --seed 0 --out /tmp/fuzz/
```

Each emitted `.py` file is accompanied by a sidecar `.tags.json` with
the syntactic-feature tags the sample actually exercised (`match`,
`try_star`, `walrus`, `fstring`, `type_params`, …) so downstream
benchmarks can break recovery rates out by feature.

## Status

Pre-release. API and CLI are still evolving with the parent project.
