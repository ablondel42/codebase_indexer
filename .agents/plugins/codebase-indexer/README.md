# Codebase Indexer & Token Saver Plugin

Workspace-scoped Antigravity plugin for `/Users/arnaud/dev/codebase_indexer` (or any clone root) providing:

- **MCP Server (`codebase-indexer_mcp` via `mcp_config.json` key `mcp`)**: Launches `./cbi serve --db-path codebase_indexer.db` over `stdio` (bootstrapping the initial DB automatically on fresh clones) for token-lean outline, slice, reference, and blast-radius queries.
- **Tier-1 Rule (`rules/AGENTS.md`)**: Routes structural navigation and blast-radius lookups to `codebase-indexer_mcp` while keeping `read_file` available where graph bodies/literals aren't indexed, sets the manual-first test policy, and isolates verbose audits into subagents.
- **Tier-2 Glob Rule (`.agents/rules/hexagonal-boundaries.md`)**: Loaded only when touching `src/` or `tests/` Python files.
- **Lifecycle Hooks (`hooks.json`)**:
  - `PreToolUse` (`scripts/pre_write_guard.py`): Validates post-edit Python AST against the 10-package Hexagonal boundaries and import/error hygiene before `edit_file` / `write_file` touches disk (`0` LLM tokens on valid edits).
  - `PostToolUse` (`scripts/post_write_sync.sh` -> `scripts/post_write_sync.py`): Incrementally re-indexes the modified Python file into `codebase_indexer.db` via `IndexerEngine.commands.process_watch_events` (`0` LLM tokens).
- **On-Demand Verification**:
  - `./cbi check [--tests [pytest-args...]]` (`scripts/check_arch.sh`) for fast Ruff + Pyright + Hexagonal AST checks.
  - `/arch-check` skill and `arch-auditor` subagent returning a `<25`-line `PASS`/`FAIL` digest.
