# Workspace Token-Saving Protocol (`codebase-indexer`)

## 1. Task-Aware Exploration: `codebase-indexer_mcp` vs. `read_file`
Use `call_mcp_tool(ServerName="codebase-indexer_mcp", ToolName=..., Arguments={...})` directly (no need to inspect MCP schema JSON files first) whenever doing **structural navigation, symbol lookup, call/reference tracing, or change-risk analysis** (use whichever `codebase-indexer*` name appears in `<mcp_servers>`):

- `get_repo_overview(repo_id?)` — repo stats and top directories
- `list_files(path_prefix?, include_tests=false, limit=100, offset=0)` — indexed `.py` paths
- `get_file_outline(file_path, include_private=true, max_depth=3)` — imports + hierarchical symbol skeleton
- `search_symbols(query, kind?, file_path?, limit=20)` — FTS5 search (`kind`: `module|class|function|method|variable`)
- `get_symbol_source(symbol, max_lines=120, offset=0)` — live line-numbered slice (`symbol`: `sym:...`, `qualified.name`, `ShortName`, or `path/file.py:Name`)
- `get_symbol_context(symbol, edge_limit=20, include_source=false, max_source_lines=80)` — parent/members/callers/callees + freshness
- `get_symbol_references(symbol, direction="incoming", refresh_lsp=false, limit=30, offset=0)` — `direction`: `incoming|outgoing|both`
- `analyze_risk(symbols)` — blast radius & risk score for `1..50` symbols

**Avoid graph blind spots (`read_file` & text search)**: `nodes_fts` and outlines index symbol names, signatures, and docstrings—not function bodies, inline string literals, comments, SQL/regex strings, or module-level dict/constant values. Use `read_file` or targeted `rg` freely whenever a task depends on body literals/comments, module-level data structures, multi-region edits in one file, full-module reviews, files under 100 lines, or non-Python files. On large `.py` files when target line spans are known, pass `StartLine` and `EndLine` to `read_file`.

**Plan hand-offs**: When writing an implementation or Orchestrator execution plan after MCP exploration, include the target `file_path` and `StartLine..EndLine` spans from MCP so execution reads tight slices instead of whole files blind.

## 2. Manual-First Test & Verification Policy
- Do **not** run `pytest` or `./cbi tests` autonomously after routine edits unless explicitly asked by the user or required by an approved implementation plan; tell the user they can run `./cbi check` (or `./cbi tests`) once finished.
- When verification *is* requested, run quiet targeted commands: `./venv/bin/ruff check <paths>`, `pyright <paths>`, `./venv/bin/pytest -q --tb=short --no-header <target>`.

## 3. Subagent Context Isolation
- Delegate multi-file architecture checks or plan review gates to `arch-auditor` (or read-only surveys to `research`) so raw command output stays outside the main conversation.
