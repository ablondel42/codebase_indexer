# Workspace Token-Saving Protocol (`repo-context`)

## 1. MCP-First Python Exploration
Prefer the `repo-context` MCP tools over full-file `read_file` or broad shell `grep`/`rg` on Python files under `src/` or `tests/`. Call `call_mcp_tool(ServerName="repo-context_repo-context", ToolName=..., Arguments={...})` directly (use whichever `repo-context*` server name appears in `<mcp_servers>`) with these exact signatures (every tool accepts optional `repo_id`):

- `get_repo_overview(repo_id?)` — repo stats and top directories
- `list_files(path_prefix?, include_tests=false, limit=100, offset=0)` — indexed `.py` paths
- `get_file_outline(file_path, include_private=true, max_depth=3)` — imports + hierarchical symbol skeleton
- `search_symbols(query, kind?, file_path?, limit=20)` — FTS5 search (`kind`: `module|class|function|method|variable`)
- `get_symbol_source(symbol, max_lines=120, offset=0)` — line-numbered slice (`symbol`: `sym:...`, `qualified.name`, `ShortName`, or `path/file.py:Name`)
- `get_symbol_context(symbol, edge_limit=20, include_source=false, max_source_lines=80)` — parent/members/callers/callees + freshness
- `get_symbol_references(symbol, direction="incoming", refresh_lsp=false, limit=30, offset=0)` — `direction`: `incoming|outgoing|both`
- `analyze_risk(symbols)` — blast radius & risk score for `1..50` symbols

Use `read_file` directly only for: (a) non-Python files, (b) files under 80 lines, or (c) exact `StartLine..EndLine` slices right before `edit_file`.

## 2. Manual-First Test & Verification Policy
- Do **not** run `pytest` or `./rc tests` autonomously after routine edits unless explicitly asked by the user or required by an approved implementation plan; tell the user they can run `./rc check` (or `./rc tests`) once finished.
- When verification *is* requested, run quiet targeted commands: `./venv/bin/ruff check <paths>`, `pyright <paths>`, `./venv/bin/pytest -q --tb=short --no-header <target>`.

## 3. Subagent Context Isolation
- Delegate multi-file architecture checks or plan review gates to `arch-auditor` (or read-only surveys to `research`) so raw command output stays outside the main conversation.
