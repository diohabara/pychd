# pcd

Python decompiler using ChatGPT

## Setup

```bash
poetry install
```

## Compile

```bash
poetry run compile <directory>
```

## Decompile

Set `OPENAI_API_KEY` environment variable. You can use `.envrc.template` as a template.

```bash
poetry run decompile <pyc-file>
```
