#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
DB_PATH="$REPO_ROOT/codebase_indexer.db"

cd "$REPO_ROOT"

if [ ! -f "$DB_PATH" ]; then
    # Keep MCP stdio stdout completely clean during initial fresh-clone indexing.
    ./cbi index . --include-tests >/dev/null
fi

exec ./cbi serve --db-path "$DB_PATH"
