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

# Sync the project venv (creates .venv if missing). `--all-packages`
# installs every uv workspace member (pychd, pychd-pyfuzz, pychd-pyobf)
# so the evaluator and tools/ scripts can import them directly.
setup:
    uv sync --all-packages

# Install prek + register pre-commit and pre-push hooks.
hooks-install:
    uvx prek install --hook-type pre-commit --hook-type pre-push

# Manually run every pre-commit + pre-push hook against all files.
hooks-run:
    uvx prek run --all-files --hook-stage pre-commit
    uvx prek run --all-files --hook-stage pre-push

# Lint (ruff check + ruff format check + ty type-check) across the
# workspace: pychd, the two PyPI-publishable members (pychd-pyfuzz,
# pychd-pyobf), and tools/ + tests/.
lint:
    uv run ruff check pychd pychd_pyfuzz pychd_pyobf tools tests
    uv run ruff format --check pychd pychd_pyfuzz pychd_pyobf tools tests
    uv run ty check pychd pychd_pyfuzz pychd_pyobf tests

# Auto-fix lint and formatting.
fix:
    uv run ruff check --fix pychd pychd_pyfuzz pychd_pyobf tools tests
    uv run ruff format pychd pychd_pyfuzz pychd_pyobf tools tests

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

# Build every third-party decompiler this repo benchmarks against
# (pycdc from source, PyLingual via podman). Optional — the
# comparison benchmark gracefully skips tools whose binaries / images
# are absent. uncompyle6 and decompyle3 are installed by `just setup`.
decompilers-build:
    bash tools/setup_decompilers.sh

# Run the comparative benchmark across every installed decompiler
# (pychd, uncompyle6, decompyle3, pycdc, pylingual). Writes
# assets/_comparison.json which `bench-figures` consumes.
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

# Build wheels for every workspace member. Per-member output goes to
# the root `dist/` directory.
build-all:
    uv build --package pychd
    uv build --package pychd-pyfuzz
    uv build --package pychd-pyobf

# Tag a pychd release and push (triggers .github/workflows/publish-pychd.yaml).
# Tag schema: `pychd-vX.Y.Z`. Use this when the bump is in pychd/.
release-pychd version:
    git tag -a "pychd-v{{ version }}" -m "pychd v{{ version }}"
    git push origin "pychd-v{{ version }}"

# Tag a pychd-pyfuzz release and push (triggers publish-pyfuzz.yaml).
release-pyfuzz version:
    git tag -a "pyfuzz-v{{ version }}" -m "pychd-pyfuzz v{{ version }}"
    git push origin "pyfuzz-v{{ version }}"

# Tag a pychd-pyobf release and push (triggers publish-pyobf.yaml).
release-pyobf version:
    git tag -a "pyobf-v{{ version }}" -m "pychd-pyobf v{{ version }}"
    git push origin "pyobf-v{{ version }}"
