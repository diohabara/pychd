#!/usr/bin/env bash
# pychd CLI walkthrough — runs the showcase module through each
# recovery tier and prints what each tier produces.
#
# Usage:
#   bash demo/walkthrough.sh
#
# Reproducible: same Python 3.14 interpreter, same showcase.py,
# rule-only output is deterministic; hybrid-rewrite output depends on
# the LLM and so we snapshot it to demo/expected/showcase.hybrid-rewrite.py
# for diffing.

set -e
cd "$(dirname "$0")/.."

GREEN='\033[0;32m'
BLUE='\033[0;34m'
DIM='\033[2m'
RESET='\033[0m'

print_step() {
    echo
    echo -e "${BLUE}━━━ $1 ━━━${RESET}"
    echo
}

print_step "Step 1: Original source"
cat demo/showcase.py

print_step "Step 2: Compile to bytecode (the .pyc that pychd will read)"
uv run python -c "
import py_compile
py_compile.compile('demo/showcase.py', cfile='/tmp/showcase.pyc', doraise=True)
print('Wrote /tmp/showcase.pyc')
import os
print(f'Size: {os.path.getsize(\"/tmp/showcase.pyc\")} bytes')
"

print_step "Step 3: rules-only recovery (deterministic, milliseconds)"
echo -e "${DIM}\$ pychd decompile /tmp/showcase.pyc --rules-only${RESET}"
echo
uv run pychd decompile /tmp/showcase.pyc --rules-only 2>/dev/null

print_step "Step 4: hybrid-rewrite recovery (one Codex call per module)"
echo -e "${DIM}\$ pychd decompile /tmp/showcase.pyc --hybrid-rewrite --backend codex${RESET}"
echo
if command -v codex >/dev/null 2>&1; then
    uv run pychd decompile /tmp/showcase.pyc --hybrid-rewrite --backend codex 2>/dev/null \
        | tee demo/expected/showcase.hybrid-rewrite.py
else
    echo -e "${DIM}(codex CLI not installed — showing cached snapshot)${RESET}"
    cat demo/expected/showcase.hybrid-rewrite.py 2>/dev/null || echo "no cached snapshot"
fi

print_step "Step 5: diff vs original"
echo -e "${DIM}\$ diff demo/showcase.py demo/expected/showcase.hybrid-rewrite.py${RESET}"
echo
if [ -f demo/expected/showcase.hybrid-rewrite.py ]; then
    diff -u demo/showcase.py demo/expected/showcase.hybrid-rewrite.py || true
else
    echo "(no hybrid-rewrite snapshot to diff)"
fi

echo
echo -e "${GREEN}Done.${RESET}"
