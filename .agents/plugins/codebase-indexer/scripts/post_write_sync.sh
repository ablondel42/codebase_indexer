#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
PAYLOAD="$(cat)"

if [ ! -x "$REPO_ROOT/venv/bin/python" ] || [ ! -f "$REPO_ROOT/codebase_indexer.db" ]; then
    printf '{}\n'
    exit 0
fi

printf '%s' "$PAYLOAD" | PYTHONPATH="$REPO_ROOT/src" exec "$REPO_ROOT/venv/bin/python" "$SCRIPT_DIR/post_write_sync.py"
