# pychd task runner — all tasks run inside Docker

image := "pychd"

# Build the Docker image
build:
    docker build -t {{ image }} .

# Run all linters (ruff check, ruff format, pyrefly)
lint: build
    docker run --rm {{ image }} uv run ruff check pychd
    docker run --rm {{ image }} uv run ruff format --check pychd
    docker run --rm {{ image }} uv run pyrefly check pychd

# Auto-fix lint issues
fix: build
    docker run --rm -v "$(pwd)":/app {{ image }} uv run ruff check --fix pychd tests
    docker run --rm -v "$(pwd)":/app {{ image }} uv run ruff format pychd tests

# Run tests
test: build
    docker run --rm {{ image }} uv run pytest tests/ -v

# Run lint + test
ci: lint test

# Open an interactive shell in the container
shell: build
    docker run --rm -it -v "$(pwd)":/app {{ image }} bash

# Compile a Python file to .pyc
compile path: build
    docker run --rm -v "$(pwd)":/app {{ image }} uv run pychd compile {{ path }}

# Decompile a .pyc file (default model: ollama/deepseek-r1)
decompile path model="ollama/deepseek-r1": build
    docker run --rm -v "$(pwd)":/app {{ image }} uv run pychd decompile {{ path }} -m {{ model }}

# Validate decompiled output against original
validate original decompiled: build
    docker run --rm -v "$(pwd)":/app {{ image }} uv run pychd validate {{ original }} {{ decompiled }}

# Tag a release and push (triggers publish workflow)
release version:
    git tag -a "v{{ version }}" -m "v{{ version }}"
    git push origin "v{{ version }}"
