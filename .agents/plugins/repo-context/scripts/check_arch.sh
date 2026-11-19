#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
cd "$REPO_ROOT"

export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

RUN_TESTS=0
if [ "${1:-}" = "--tests" ]; then
    RUN_TESTS=1
    shift
fi

"$REPO_ROOT/venv/bin/ruff" check src/ tests/ .agents/plugins/repo-context/scripts/*.py
pyright
pyright .agents/plugins/repo-context/scripts/*.py
"$REPO_ROOT/venv/bin/pytest" -q --no-header \
    tests/test_engine_and_architecture.py \
    -k TestHexagonalArchitectureBoundaryGate

if [ "$RUN_TESTS" -eq 1 ]; then
    "$REPO_ROOT/venv/bin/pytest" -q --tb=short --no-header ${@+"$@"}
fi
