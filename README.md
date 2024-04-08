# PyChD

[![CI](https://github.com/diohabara/pychd/actions/workflows/ci.yml/badge.svg)](https://github.com/diohabara/pychd/actions/workflows/ci.yml)
[![PyPI Version](https://img.shields.io/pypi/v/pychd.svg)](https://pypi.python.org/pypi/pychd)

The ChatGPT-powered decompiler for Python, providing superior code analysis capabilities

## Usage

### Install

From pip

```bash
pip install pychd
```

### Compile

```bash
pychd compile <directory | file> # you need to specify a directory or a .py file
```

E.g.,

```bash
pychd compile examples/01_example_variables.py # `example/__pycache__/01_example_variables.cpython-310.pyc` will be created
```

### Decompile

```bash
pychd decompile <pyc-file> # you need to specify a .pyc file
```

E.g.,

```bash
pychd decompile example/__pycache__/01_example_variables.cpython-310.pyc # decompiled code will be printed
```

```bash
pychd decompile example/__pycache__/01_example_variables.cpython-310.pyc -o example/decompiled/01_example_variables.cpython-310.py # decompiled code will be written to `example/decompiled/01_example_variables.cpython-310.py`
```

## Examples

You can find examples in `example` directory.

## Development

### Setup

1. Install [rye](https://rye.astral.sh/guide/installation/).

2. Install all dependencies.

```bash
rye sync --all-features
```

3. Set `OPENAI_API_KEY` environment variable. If you're using `direnv`, you can use `.envrc.template` as a template.

4. Run the compiler/decompiler.

Using scripts:

```bash
rye run python -m pychd.main compile examples/01_example_variables.py # compile
```

```bash
rye run python -m pychd.main decompile example/__pycache__/01_example_variables.cpython-310.pyc # decompile
```

Activating the virtual environment:

```bash
. .venv/bin/activate
```

```bash
python -m pychd.main compile example/python/01_example_variables.py # compile
```

```bash
python -m pychd.main decompile example/python/01_example_variables.pyc # decompile
```
