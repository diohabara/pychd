#!/usr/bin/env bash
# Build the third-party decompilers that ``tools/compare_decompilers.py``
# scores against, so a reviewer can reproduce the comparison numbers
# locally from a clean checkout.
#
# We pin every binary / container by **git commit** rather than by tag
# — pycdc has no release cadence and PyLingual ships through git too;
# without a pinned SHA the benchmark would silently drift whenever
# upstream merges a PR.
#
# uncompyle6 and decompyle3 are pinned via ``pyproject.toml`` (PyPI
# version locks) and don't need to be rebuilt here. This script
# specifically handles toolchains the project venv can't host (C++
# binaries; PyTorch-heavy ML decompilers).
#
# Usage::
#
#     bash tools/setup_decompilers.sh             # builds everything
#     bash tools/setup_decompilers.sh pycdc       # just pycdc
#     bash tools/setup_decompilers.sh pylingual   # just the podman image
#     bash tools/setup_decompilers.sh --clean     # remove build tree + image
#
# Output locations::
#
#     /tmp/pychd-decompilers/pycdc/build/pycdc
#     /tmp/pychd-decompilers/pycdc/build/pycdas
#     podman image  pychd-pylingual:latest
set -euo pipefail

# Pin pycdc to a specific commit so reviewers get the same binary on
# every reproduction. Update intentionally when bumping.
PYCDC_COMMIT="b428976"
# PyLingual revision baked into the podman image. Update intentionally.
PYLINGUAL_REV="${PYLINGUAL_REV:-main}"
ROOT="/tmp/pychd-decompilers"
# Container engine — defaults to podman (rootless); set
# ``PYCHD_CONTAINER=docker`` to use Docker on hosts where podman isn't
# available.
CONTAINER="${PYCHD_CONTAINER:-podman}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
    cat <<EOF
Usage: $0 [pycdc|pylingual|all] [--clean]

Build the third-party decompilers used by tools/compare_decompilers.py.

Targets:
  pycdc      Build the C++ ``pycdc`` / ``pycdas`` binaries
             into ${ROOT}/pycdc/build/ (pinned @ ${PYCDC_COMMIT}).
  pylingual  Build the rootless ${CONTAINER} image ``pychd-pylingual``
             (pinned to PyLingual @ ${PYLINGUAL_REV}; ~2 GB image,
             first build downloads ML model weights).
  all        Build every target above (default).

uncompyle6 / decompyle3 are pip-installed via the project's lockfile;
this script only handles toolchains the venv can't host.

Options:
  --clean       Remove ${ROOT} and the ${CONTAINER} image, then exit.
  -h, --help    Show this message.

Environment:
  PYCHD_CONTAINER  podman (default) | docker
  PYLINGUAL_REV    git ref baked into the image (default: ${PYLINGUAL_REV})
EOF
}

TARGET="all"
CLEAN="false"
for arg in "$@"; do
    case "$arg" in
        -h|--help) usage; exit 0 ;;
        --clean) CLEAN="true" ;;
        pycdc|pylingual|all) TARGET="$arg" ;;
        *) echo "error: unknown argument: $arg" >&2; usage; exit 1 ;;
    esac
done

if [[ "${CLEAN}" == "true" ]]; then
    echo "removing ${ROOT}"
    rm -rf "${ROOT}"
    if command -v "${CONTAINER}" >/dev/null 2>&1; then
        if "${CONTAINER}" image exists pychd-pylingual 2>/dev/null; then
            echo "removing ${CONTAINER} image pychd-pylingual"
            "${CONTAINER}" rmi pychd-pylingual >/dev/null
        fi
    fi
    exit 0
fi

build_pycdc() {
    # Locate a cmake binary. Prefer the project venv (uv pip install
    # cmake) so we don't depend on a system-wide CMake install.
    local cmake_bin=""
    if [[ -x "${REPO_ROOT}/.venv/bin/cmake" ]]; then
        cmake_bin="${REPO_ROOT}/.venv/bin/cmake"
    elif command -v cmake >/dev/null 2>&1; then
        cmake_bin="$(command -v cmake)"
    else
        echo "error: no cmake found." >&2
        echo "  Install it with:  uv pip install cmake" >&2
        return 1
    fi
    echo "using cmake: ${cmake_bin}"

    if ! command -v c++ >/dev/null 2>&1 && ! command -v g++ >/dev/null 2>&1; then
        echo "error: no C++ compiler in PATH (need g++ or clang++)." >&2
        return 1
    fi

    mkdir -p "${ROOT}"
    cd "${ROOT}"
    if [[ ! -d pycdc ]]; then
        echo "cloning pycdc..."
        git clone --quiet https://github.com/zrax/pycdc.git
    fi
    cd pycdc
    # ``git fetch`` so the pinned SHA is available even on a shallow
    # clone from a previous run; ignore failure (commit may already be
    # local).
    git fetch --quiet --depth 50 origin "${PYCDC_COMMIT}" 2>/dev/null || true
    git checkout --quiet "${PYCDC_COMMIT}"
    local pycdc_sha
    pycdc_sha="$(git rev-parse HEAD)"
    local pycdc_date
    pycdc_date="$(git log -1 --format='%cd' --date=short)"

    mkdir -p build
    cd build
    echo "configuring pycdc..."
    "${cmake_bin}" .. >/dev/null
    echo "building pycdc..."
    make -j"$(nproc 2>/dev/null || echo 4)" >/dev/null

    if [[ ! -x pycdc ]]; then
        echo "error: build completed but pycdc binary missing" >&2
        return 1
    fi
    cat <<EOF

built pycdc:
  commit     ${pycdc_sha:0:7} (${pycdc_date})
  binary     ${ROOT}/pycdc/build/pycdc
  binary     ${ROOT}/pycdc/build/pycdas
EOF
}

build_pylingual() {
    if ! command -v "${CONTAINER}" >/dev/null 2>&1; then
        echo "error: ${CONTAINER} not in PATH." >&2
        echo "  Install podman, or set PYCHD_CONTAINER=docker." >&2
        return 1
    fi
    echo "building ${CONTAINER} image: pychd-pylingual (PyLingual @ ${PYLINGUAL_REV})"
    echo "  first build downloads PyTorch + HuggingFace weights (~2 GB) and"
    echo "  may take 5-15 minutes; subsequent rebuilds use layer cache."
    "${CONTAINER}" build \
        --tag pychd-pylingual:latest \
        --build-arg "PYLINGUAL_REV=${PYLINGUAL_REV}" \
        -f "${REPO_ROOT}/tools/pylingual.Containerfile" \
        "${REPO_ROOT}"

    # Capture the actual revision baked into the image so the
    # comparison harness can report it without re-querying github.
    local rev
    rev="$("${CONTAINER}" run --rm --entrypoint cat pychd-pylingual:latest \
        /opt/pylingual-rev.txt 2>/dev/null || echo "${PYLINGUAL_REV}")"
    cat <<EOF

built pylingual:
  image      pychd-pylingual:latest
  commit     ${rev:0:7}
EOF
}

case "${TARGET}" in
    pycdc)      build_pycdc ;;
    pylingual)  build_pylingual ;;
    all)        build_pycdc; echo; build_pylingual ;;
esac

cat <<EOF

tools/compare_decompilers.py will pick the built tools up automatically.
EOF
