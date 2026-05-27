# pychd-pyobf

Anonymise identifiers, string constants, docstrings, and metadata
inside a CPython `.pyc` while preserving the opcode stream exactly.

Built to neutralise LLM training-data memorisation when benchmarking
Python decompilers: even if an LLM has seen the original source on
the internet, the anonymised `.pyc` does not contain the surface
tokens (variable names, comments, docstrings) it would use to
recognise the source.

Covers every CPython release pychd recognises: 3.0–3.14.
- 3.14 (the running interpreter) is rewritten natively via
  `types.CodeType.replace()`.
- 3.0–3.13 are rewritten via a subprocess into a uv-managed Python of
  that minor version, so the obfuscator stays a tiny dependency.

Pair with `pychd-pyfuzz` (random valid-Python source generator) for
the strongest available contamination guarantee.

See the main [pychd README](https://github.com/diohabara/pychd) for
the broader story.

## Install

```bash
pip install pychd-pyobf
```

## Use

```bash
pychd-pyobf rewrite IN.pyc OUT.pyc --mapping mapping.json
```

The `--mapping` flag (optional) writes the original-to-anonymised
identifier dict to JSON for audit / debugging. Without it, the
mapping is discarded after rewriting.

## Status

Pre-release. API and CLI are still evolving with the parent project.
