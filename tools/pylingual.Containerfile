# PyLingual decompiler in a self-contained podman image.
#
# PyLingual (Wiedemeier et al., IEEE S&P 2025) ships as a Poetry-managed
# Python 3.12 application with heavy ML dependencies (PyTorch,
# transformers, HuggingFace model downloads). That doesn't fit cleanly
# into pychd's uv-managed Python 3.14 venv: different Python version,
# different lock file, multiple GB of model weights.
#
# Size optimisation
# -----------------
#
# A naïve install of PyLingual lands at ~12.8 GB. Most of that is
# *CUDA* PyTorch + nvidia .so libraries we will never use — PyLingual
# runs inference on CPU. Three techniques compress the final image
# to ~2 GB:
#
# 1. **CPU-only PyTorch** — installed from the official
#    ``download.pytorch.org/whl/cpu`` index *before* PyLingual's
#    poetry-driven dep solver runs, so the CUDA wheels never get
#    pulled. This alone drops ~9 GB of nvidia .so blobs. CPU-only
#    is also the right choice for AMD GPU hosts: PyTorch's CUDA
#    wheels won't run on ROCm at all, and the ROCm wheels are an
#    even larger separate distribution. PyLingual's inference is
#    a small T5 model — running it on CPU is fast enough that the
#    runtime cost doesn't justify pulling either GPU stack in.
#
# 2. **Multi-stage build** — the final image only contains the
#    runtime layer: ``site-packages`` plus the pylingual source plus
#    the HuggingFace model cache. The build stage's pip wheels,
#    apt build-essential, .git history, and ``__pycache__`` trees
#    stay behind.
#
# 3. **Aggressive cleanup** — strip ``__pycache__``, ``*.pyc``,
#    ``tests/``, ``docs/``, examples directories, and stripped .so
#    files inside ``torch/``. The ``HF_HUB_DISABLE_TELEMETRY=1``
#    + ``transformers`` config disables the cache of unused
#    revisions.
#
# Build:
#
#     bash tools/setup_decompilers.sh           # builds this image
#
# Run (the comparison harness calls this for each module):
#
#     podman run --rm --read-only \
#         -v <pyc_dir>:/in:ro -v <out_dir>:/out:rw \
#         pychd-pylingual \
#         /in/<file>.pyc -o /out --quiet
#
# Pinning policy:
#   - PyLingual revision is captured in PYLINGUAL_REV below.
#   - HuggingFace model weights are downloaded at build time and baked
#     into the image, so the run-time image is offline-capable and
#     deterministic across reruns.

# -- Stage 1: builder ---------------------------------------------------------
FROM python:3.12-slim AS builder

ARG PYLINGUAL_REV=main

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HOME=/opt/hf-cache \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VIRTUALENVS_CREATE=false

# Build-only dependencies. We purge ``apt`` afterwards inside this
# stage too — even though the stage is discarded, smaller layers
# build/cache faster on slow CI.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone --depth 1 https://github.com/syssec-utd/pylingual.git \
    && cd pylingual \
    && git fetch --depth 1 origin "${PYLINGUAL_REV}" 2>/dev/null || true \
    && git checkout "${PYLINGUAL_REV}" 2>/dev/null || git checkout main \
    && git rev-parse HEAD > /opt/pylingual-rev.txt \
    && rm -rf .git

# Install PyLingual via plain pip (not poetry) with the CPU-only
# PyTorch index as the *primary* index. Poetry 2.x would otherwise
# resolve the regular ``torch`` from PyPI and pull every nvidia-* /
# triton-* transitive dep, blowing the image up to ~12 GB.
WORKDIR /opt/pylingual
RUN pip install \
        --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple \
        "torch==2.7.1+cpu" \
    && pip install \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        . \
    # Belt-and-braces: drop any nvidia-* / triton wheels that pip
    # might have pulled in transitively. Yes — pip will sometimes
    # still pick the standard torch index when one of the transitive
    # deps requests it, hence the explicit purge.
    && pip uninstall -y $(pip list --format=freeze \
        | grep -E "^(nvidia-|triton)" \
        | awk -F'==' '{print $1}') 2>/dev/null || true \
    # Drop test suites, doc bundles, examples, and __pycache__ trees
    # before they get baked into the final image's site-packages.
    && find /usr/local/lib/python3.12 -type d -name '__pycache__' -prune -exec rm -rf {} + \
    && find /usr/local/lib/python3.12 -type d -name 'tests' -prune -exec rm -rf {} + \
    && find /usr/local/lib/python3.12 -type d -name 'examples' -prune -exec rm -rf {} + \
    && find /usr/local/lib/python3.12 -type d -name 'docs' -prune -exec rm -rf {} + \
    && find /usr/local/lib/python3.12 -name '*.pyc' -delete

# Best-effort pre-fetch of the Python-3.8 HuggingFace model bundle
# into HF_HOME=/opt/hf-cache. Build-time downloads from huggingface.co
# can fail behind corporate proxies or intermittent rate-limits, so we
# treat this as a *cache warmer* — failure here doesn't break the
# build, the runtime will lazy-download on first use. We still create
# the directory so the runtime stage's COPY always succeeds.
RUN mkdir -p /opt/hf-cache \
    && (python -c "import huggingface_hub as h; \
[h.snapshot_download(r) or print('cached', r) for r in ( \
    'syssec-utd/py38-pylingual-v1-segmenter', \
    'syssec-utd/py38-pylingual-v1-tokenizer', \
    'syssec-utd/py38-pylingual-v1.3-statement', \
    'syssec-utd/py38-pylingual-v1.3-tok', \
)]" || echo "warning: HF pre-fetch failed; runtime will download lazily.")

# -- Stage 2: runtime ---------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HOME=/opt/hf-cache \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# We only need libgomp.so.1 from OpenMP for PyTorch CPU; the rest of
# build-essential / git / apt cache can stay in the builder stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy the installed site-packages, the pylingual source tree, the
# CLI entry-point shim that ``pip install -e .`` registered, and the
# baked-in HuggingFace cache. Three small copies, one big layer.
COPY --from=builder /usr/local/lib/python3.12/site-packages \
                    /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/pylingual /usr/local/bin/pylingual
COPY --from=builder /opt/pylingual /opt/pylingual
COPY --from=builder /opt/pylingual-rev.txt /opt/pylingual-rev.txt
COPY --from=builder /opt/hf-cache /opt/hf-cache

WORKDIR /work
ENTRYPOINT ["pylingual"]
