# pychd task runner — runs everything via `uv` (no Docker required).
#
# Bootstrap once:
#   just setup
#
# Day-to-day:
#   just lint            # ruff + ty
#   just test            # pytest
#   just ci              # lint + test
#   just decompile X     # decompile a file or directory
#   just compile X       # compile a .py file/tree to .pyc

default:
    @just --list

# Sync the project venv (creates .venv if missing).
setup:
    uv sync

# Install prek + register pre-commit and pre-push hooks.
hooks-install:
    uvx prek install --hook-type pre-commit --hook-type pre-push

# Manually run every pre-commit + pre-push hook against all files.
hooks-run:
    uvx prek run --all-files --hook-stage pre-commit
    uvx prek run --all-files --hook-stage pre-push

# Lint (ruff check + ruff format check + ty type-check).
lint:
    uv run ruff check pychd tests
    uv run ruff format --check pychd tests
    uv run ty check pychd tests

# Auto-fix lint and formatting.
fix:
    uv run ruff check --fix pychd tests
    uv run ruff format pychd tests

# Run pytest.
test:
    uv run pytest tests/ -v

# Lint + test (the gate prek runs on pre-push).
ci: lint test

# Compile a Python file or directory to .pyc.
compile path:
    uv run pychd compile {{ path }}

# Decompile a .pyc, .py, or directory tree.
#
# Modes:
#   just decompile X                            # hybrid (default model: ollama/deepseek-r1)
#   just decompile X --rules-only               # rule-based skeleton, no LLM
#   just decompile X --llm-only -m gpt-4o       # full LLM pipeline
#
# Pass extra args after the positional path, e.g.:
#   just decompile foo.pyc --rules-only -o out.py
decompile *args:
    uv run pychd decompile {{ args }}

# Quick alias for rule-only decompilation (no LLM call).
decompile-rules path output="":
    uv run pychd decompile {{ path }} --rules-only {{ if output != "" { "-o " + output } else { "" } }}

# Validate decompiled output against the original source.
validate original decompiled *args:
    uv run pychd validate {{ original }} {{ decompiled }} {{ args }}

# Build corpora into /tmp/pychd-corpora/ (cached; --force to refresh).
bench-setup:
    uv run python tools/build_corpora.py

# Run a single corpus benchmark and print markdown.
bench-stdlib: bench-setup
    uv run python tools/benchmark.py /tmp/pychd-corpora/stdlib \
        --top-level-only --corpus-label "Python 3.14 stdlib (curated)" --chart

bench-pypi: bench-setup
    uv run python tools/benchmark.py /tmp/pychd-corpora/pypi \
        --corpus-label "PyPI (requests/click/attrs/flask/httpx/rich)" --chart

bench-cursor: bench-setup
    uv run python tools/benchmark.py /tmp/pychd-corpora/cursor-sdk \
        --top-level-only --corpus-label "cursor-sdk 0.1.5 (top-level modules)" --chart

# Run all benchmarks back-to-back.
bench: bench-stdlib bench-pypi bench-cursor

# Generate cross-version .pyc fixtures (one per locally-installed Python).
bench-versions:
    uv run python tools/build_multiversion_fixtures.py
    uv run pytest tests/test_versions.py -v

# Run the comparative benchmark (pychd vs uncompyle6 vs decompyle3).
# Writes assets/_comparison.json consumed by `bench-figures`.
bench-compare:
    uv run python tools/compare_decompilers.py

# Render every SVG figure under assets/ from the latest results +
# comparison JSON. Idempotent — safe to re-run.
bench-figures:
    uv run python tools/render_figures.py

# Full reproducibility: build corpora, run all tests + lint + benchmarks,
# regenerate README's paper-generated block AND every figure under
# assets/. The single entry point for anyone trying to reproduce the
# published results.
paper: setup bench-setup
    uv run pytest tests/ -q
    uv run ruff check pychd tests
    uv run ruff format --check pychd tests
    uv run ty check pychd tests
    uv run python tools/render_paper.py --render-figures
    @echo
    @echo "✅ paper regenerated. README §5.3 + assets/*.svg up to date."
    @echo "   Inspect changes with: git diff README.md assets/"

# Tag a release and push (triggers the publish workflow).
release version:
    git tag -a "v{{ version }}" -m "v{{ version }}"
    git push origin "v{{ version }}"
