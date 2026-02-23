# PyChD

[![CI](https://github.com/diohabara/pychd/actions/workflows/ci.yml/badge.svg)](https://github.com/diohabara/pychd/actions/workflows/ci.yml)
[![PyPI Version](https://img.shields.io/pypi/v/pychd.svg)](https://pypi.python.org/pypi/pychd)

LLM-powered Python bytecode decompiler. Uses [litellm](https://github.com/BerriAI/litellm) to support OpenAI, Anthropic, Google, Ollama, and other providers. Handles `.pyc` files from any Python version via [xdis](https://github.com/rocky/python-xdis).

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/)
- [just](https://github.com/casey/just) (task runner)

## Quick start

```bash
just build          # build the Docker image
just lint           # run linters (ruff, pyrefly)
just test           # run tests
just ci             # lint + test
just shell          # interactive shell inside the container
```

## Install (pip)

```bash
pip install pychd
```

## Usage

### Compile

```bash
pychd compile <directory | file>
```

```bash
just compile example/python/01_example_variables.py
```

### Decompile

```bash
pychd decompile <pyc-file> [-m MODEL] [-o OUTPUT]
```

```bash
# Default model (ollama/deepseek-r1)
just decompile example/__pycache__/01_example_variables.cpython-314.pyc

# Specify a model
just decompile example/__pycache__/01_example_variables.cpython-314.pyc gpt-4o
```

Supported `-m` values include any model supported by litellm:

| Provider | Example |
|----------|---------|
| OpenAI | `gpt-4o` |
| Anthropic | `claude-sonnet-4-20250514` |
| Google | `gemini/gemini-2.0-flash` |
| Ollama (local) | `ollama/deepseek-r1`, `ollama/llama3` |

Large disassembly is automatically split into token-safe chunks when it exceeds the model's context window.

### Validate

Compare original source against decompiled output using AST comparison:

```bash
pychd validate <original> <decompiled> [-v]
```

```bash
just validate example/python/ example/decompiled/
```

## Development

All development tasks run inside Docker via `just`. No local Python installation is required.

```bash
just fix            # auto-fix lint issues
just test           # run pytest
just shell          # drop into the container
```

## Examples

Example Python source files are in `example/python/`. Pre-generated decompiled output is in `example/decompiled/`.
