# pcd

Python decompiler using ChatGPT

## Setup

```bash
poetry install
poetry run pre-commit install
```

Set `OPENAI_API_KEY` environment variable. If you're using `direnv`, you can use `.envrc.template` as a template.
Put `src/pcd/logging.conf`. You can copy `src/pcd/logging.conf.template` like this:

```bash
cp src/pcd/logging.conf.template src/pcd/logging.conf
```

## Compile

```bash
poetry run pcd compile <directory | file> # you need to specify a directory or a file
```

## Decompile

```bash
poetry run pcd decompile [pyc-file] # you can specify a pyc file
```

## Examples

You can find examples in `examples` directory.

| Original | Decompiled |
| --- | --- |
| [](./example/example_simple.py) | [](./example/decompiled_example_simple.py) |
| [](./example/example_if_else.py) | [](./example/decompiled_example_if_else.py) |
