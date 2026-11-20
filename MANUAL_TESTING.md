# Manual Testing Guide (`./cbi`)

Single-command terminal reference covering every `./cbi` bootstrapper command, CLI subcommand, flag variation, interactive `cbi>` shell command, daemon, security guard, and all **9** MCP tools (`codebase-indexer_mcp`).

Run commands one by one from the repository root (`/Users/arnaud/dev/codebase_indexer`).

---

## 1. Bootstrapper, Linting & Test Commands (`./cbi`)

### 1.1 Show Quickstart Menu
```bash
./cbi
```
**Expected:** Prints the `./cbi` usage menu (`path/to/repo`, `demo`, `dev`, `check`, `tests`) and subcommand forwarding hint.

### 1.2 Show CLI Version
```bash
./cbi --version
```
**Expected:** Prints `cbi 0.2.0`.

### 1.3 Show Root CLI Help & All Subcommands
```bash
./cbi --help
```
**Expected:** Lists global options (`--db-path`, `--verbose`, `--debug`) and all subcommands (`index`, `db`, `symbols`, `search`, `files`, `outline`, `source`, `refs`, `risk`, `shell`, `watch`, `serve`, `mcp-call`).

### 1.4 Full Dev Rebuild (Recreate DB + AST + UTF-8 Text Corpus + Pyright LSP)
```bash
./cbi dev
```
**Expected:** Deletes and recreates `./codebase_indexer.db` (`SCHEMA_VERSION = 3`) with `--include-tests --force --lsp`, indexing all `.py` AST symbols, all UTF-8 repository text files (`text_files` / `text_fts`), and Pyright LSP references.

### 1.5 Fast Architecture, Linter & Typecheck Gate
```bash
./cbi check
```
**Expected:** Runs Ruff (`All checks passed!`), Pyright (`0 errors`), and `TestHexagonalArchitectureBoundaryGate` (`5 passed`).

### 1.6 Architecture Gate + Targeted Pytest Subset
```bash
./cbi check --tests -q --tb=short --no-header tests/test_code_search_stress.py
```
**Expected:** Runs the full `./cbi check` gate followed by the 4 adversarial `search_code` stress suites (`4 passed`).

### 1.7 Run Full Test Suite
```bash
./cbi tests -q --tb=short --no-header
```
**Expected:** Runs `ruff check` followed by the entire pytest suite (`207 passed`).

### 1.8 Shorthand Repo Index Without Interactive Shell
```bash
./cbi . --no-cli
```
**Expected:** Incrementally indexes `.` (excluding `tests/`) into `./codebase_indexer.db` and exits cleanly.

### 1.9 Demo Mode (Index Repo Including Tests Without Interactive Shell)
```bash
./cbi demo --no-cli
```
**Expected:** Incrementally indexes `.` with `--include-tests` into `./codebase_indexer.db` and exits cleanly.

---

## 2. Repository Indexing (`./cbi index`)

### 2.1 Show `index` Subcommand Help
```bash
./cbi index --help
```
**Expected:** Displays `cbi index` flags (`--force`, `--include-tests`, `--lsp`, `--shell`).

### 2.2 Incremental Index (Hash & Stat Diffed)
```bash
./cbi index . --include-tests
```
**Expected:** Emits JSON `BatchIndexResult` skipping unchanged `.py` and text files (`"files_unchanged"`).

### 2.3 Forced Full Re-Extraction (`--force`)
```bash
./cbi index . --include-tests --force
```
**Expected:** Re-parses and re-persists all Python files and UTF-8 text documents even when hashes match.

### 2.4 Index With Pyright LSP Reference Enrichment (`--lsp`)
```bash
./cbi index . --include-tests --lsp
```
**Expected:** Runs AST + UTF-8 text indexing followed by Pyright LSP reference enrichment (`"lsp": {"available": true, ...}`).

---

## 3. Database Inspection (`./cbi db ...`)

### 3.1 Show `db` Group Help
```bash
./cbi db --help
```
**Expected:** Lists `status`, `repos`, `tables`, `runs`, and `sql`.

### 3.2 Database & Graph Status (`cbi db status`)
```bash
./cbi db status
```
**Expected:** Emits JSON with `"schema_ok": true`, DB file size, active repository stats, node kind counts, and edge kind counts.

### 3.3 List Indexed Repositories (`cbi db repos`)
```bash
./cbi db repos
```
**Expected:** Lists every indexed repository in `./codebase_indexer.db` with `id`, `name`, `root_path`, `last_indexed_at`, and node/file totals.

### 3.4 List SQLite Tables & Row Counts (`cbi db tables`)
```bash
./cbi db tables
```
**Expected:** Shows `"user_version": 3` and row counts for `repos`, `files`, `text_files`, `text_fts`, `nodes`, `nodes_fts`, `edges`, `index_runs`, and `reference_refresh`.

### 3.5 List Recent Indexing Runs (`cbi db runs`)
```bash
./cbi db runs --limit 5
```
**Expected:** Lists up to 5 recent `index_runs` records with timestamps, `"status": "completed"`, and run metrics.

### 3.6 Read-Only SQL Over Indexed UTF-8 Text Corpus (`cbi db sql`)
```bash
./cbi db sql "SELECT language, COUNT(*) AS files, SUM(line_count) AS lines FROM text_files GROUP BY language ORDER BY files DESC"
```
**Expected:** Returns per-language file and line totals (`python`, `markdown`, `shell`, `json`, `toml`, `text`) from `text_files`.

### 3.7 Read-Only SQL Over AST Symbol Nodes (`cbi db sql`)
```bash
./cbi db sql "SELECT kind, COUNT(*) AS count FROM nodes GROUP BY kind ORDER BY count DESC"
```
**Expected:** Returns symbol counts grouped by `SymbolKind`.

### 3.8 Verify SQLite Authorizer Blocks Mutating SQL (`cbi db sql`)
```bash
./cbi db sql "DELETE FROM nodes"
```
**Expected:** Rejected by the SQLite read-only authorizer with a JSON `"not authorized"` error on `stderr` and non-zero exit code `1`.

---

## 4. Whole-Codebase Text & Regex Search (`./cbi search`)

### 4.1 Show `search` Subcommand Help
```bash
./cbi search --help
```
**Expected:** Displays `query`, `--repo`, `--regex`, `--case-sensitive`, `--path`, `--lang`, `--no-tests`, `-C/--context`, `--limit`, and `--offset`.

### 4.2 Literal Full-Codebase Search With Python Symbol Attribution
```bash
./cbi search "TextSearchRepositoryProtocol"
```
**Expected:** Uses FTS5 trigram pruning (`"trigram_pruned": true`), lists all matching files in `file_counts`, and enriches Python hits with `symbol_id`, `qualified_name`, `symbol_kind`, and `symbol_lines`.

### 4.3 Regex Word-Boundary Search Across Code, Docs & Scripts (`--regex`)
```bash
./cbi search '\bcbi\b' --regex --limit 10
```
**Expected:** Finds `\bcbi\b` across `.md`, `cbi`, `pyproject.toml`, `.agents/**`, `src/**`, and `tests/**`, returning all matching files in `file_counts` and the first 10 matches.

### 4.4 Short `<3`-Character Fallback Search
```bash
./cbi search "ui" --limit 10
```
**Expected:** Automatically falls back to scanning `text_files.content` (`"trigram_pruned": false`) with zero missed matches.

### 4.5 Case-Sensitive Search (`--case-sensitive`)
```bash
./cbi search "UnitOfWorkProtocol" --case-sensitive --limit 10
```
**Expected:** Matches exact casing `UnitOfWorkProtocol` only.

### 4.6 Path-Prefix & Language Filter Over `.agents/` Docs (`--path`, `--lang`)
```bash
./cbi search "search_code" --path .agents --lang markdown
```
**Expected:** Restricts search to Markdown files under `.agents/` (such as `.agents/plugins/codebase-indexer/rules/AGENTS.md`).

### 4.7 Exclude Tests & Include Context Lines (`--no-tests`, `-C`)
```bash
./cbi search "CodeSearchService" --no-tests -C 2 --limit 5 --offset 0
```
**Expected:** Excludes `tests/**` and attaches `context_before` and `context_after` (up to 2 lines each) around every match.

---

## 5. Tracked Python Files & Structural Outlines (`./cbi files`, `./cbi outline`)

### 5.1 List Tracked Python Files With Freshness (`cbi files`)
```bash
./cbi files --limit 20
```
**Expected:** Lists tracked non-test `.py` files with `line_count`, `symbol_count`, `is_test: false`, and `"freshness": "fresh"`.

### 5.2 Filter Tracked Files by Glob Pattern & Include Tests (`cbi files`)
```bash
./cbi files "*search*" --include-tests --limit 20
```
**Expected:** Lists tracked `.py` files matching `*search*` across both `src/` and `tests/`.

### 5.3 Full Hierarchical File Outline (`cbi outline`)
```bash
./cbi outline src/engine/services/facade.py
```
**Expected:** Emits deduplicated `imports` and nested symbol tree (`_EngineRuntime`, `IndexerEngine`, methods, line spans, signatures, docstrings).

### 5.4 Public-Only Bounded-Depth File Outline (`--no-private`, `--max-depth`)
```bash
./cbi outline src/mcp_server/services/registry.py --no-private --max-depth 2
```
**Expected:** Omits private (`_`-prefixed) helpers and caps symbol nesting depth at `2`.

---

## 6. Symbol Search, Browser, Context & Source (`./cbi symbols ...`, `./cbi source`)

### 6.1 FTS5 Symbol Search (`cbi symbols search`)
```bash
./cbi symbols search CodeSearchService
```
**Expected:** Returns ranked `SymbolSummary` matches from `nodes_fts` with `sym:...` IDs, line spans, and signatures.

### 6.2 Filtered Symbol Search by Kind & Path (`--kind`, `--file`)
```bash
./cbi symbols search search --kind method --file src/context/ --limit 10
```
**Expected:** Returns only `method` symbols under `src/context/` matching `search`.

### 6.3 Paginated Symbol Browser (`cbi symbols list`)
```bash
./cbi symbols list --kind class --file src/mcp_server/ --limit 15 --offset 0
```
**Expected:** Lists `class` symbols in `src/mcp_server/` with `total_count`, `returned_count`, and `has_more`.

### 6.4 Bounded Symbol Context Neighborhood (`cbi symbols show`)
```bash
./cbi symbols show IndexerEngine
```
**Expected:** Returns `focus_symbol`, structural `children`, incoming/outgoing relationship edges, counts, and disk freshness.

### 6.5 Symbol Context With Embedded Source (`cbi symbols show --source`)
```bash
./cbi symbols show CodeSearchService.search --source --max-source-lines 40
```
**Expected:** Includes live line-numbered source code inside `focus_symbol.source`.

### 6.6 Line-Numbered Symbol Source Slice (`cbi source`)
```bash
./cbi source CodeSearchService.search --max-lines 35 --offset 0
```
**Expected:** Emits `SymbolSnippet` JSON with 1-based line-numbered `source`, `start_line`, `end_line`, `truncated`, and `is_fresh`.

---

## 7. Reference Tracing & Change Risk (`./cbi refs`, `./cbi risk`)

### 7.1 Incoming & Outgoing Symbol References (`cbi refs`)
```bash
./cbi refs SearchCodeInput --direction both --limit 20 --offset 0
```
**Expected:** Lists incoming and outgoing AST/LSP reference sites with `file_path`, `line`, `kind`, and 1-line trimmed source preview.

### 7.2 Live Pyright LSP Reference Refresh (`cbi refs --refresh`)
```bash
./cbi refs CodeSearchService.search --direction incoming --refresh
```
**Expected:** Refreshes Pyright LSP references on demand for `CodeSearchService.search` and returns updated incoming callers/references.

### 7.3 Single-Symbol Change Risk Analysis (`cbi risk`)
```bash
./cbi risk CodeSearchService
```
**Expected:** Computes deterministic `risk_score` (`0–100`), `decision` (`safe_enough` / `review_required` / `high_risk`), `factors`, and blast-radius `facts`.

### 7.4 Multi-Symbol Combined Change Risk Analysis (`cbi risk`)
```bash
./cbi risk SearchCodeInput IndexerEngine
```
**Expected:** Evaluates combined cross-file and cross-module blast radius across both symbols.

---

## 8. All 9 MCP Tools Individually (`./cbi mcp-call`)

Each command invokes the in-process `McpToolRegistry` and prints payload `bytes`, `estimated_tokens`, and the compact MCP JSON response.

### 8.1 MCP Tool 1 — `get_repo_overview`
```bash
./cbi mcp-call get_repo_overview
```
**Expected:** Returns compact repo metadata, node/edge counts, top non-test directories, and recent runs.

### 8.2 MCP Tool 2 — `list_files`
```bash
./cbi mcp-call list_files src/mcp_server limit=10 include_tests=false
```
**Expected:** Returns paginated tracked `.py` paths under `src/mcp_server`.

### 8.3 MCP Tool 3 — `get_file_outline`
```bash
./cbi mcp-call get_file_outline src/mcp_server/models/inputs.py max_depth=2
```
**Expected:** Returns compact imports and symbol skeleton for `src/mcp_server/models/inputs.py`.

### 8.4 MCP Tool 4 — `search_symbols`
```bash
./cbi mcp-call search_symbols SearchCodeInput limit=5
```
**Expected:** Returns compact ranked symbol declarations matching `SearchCodeInput`.

### 8.5 MCP Tool 5 — `search_code` (Key-Value Shorthand)
```bash
./cbi mcp-call search_code TextSearchRepositoryProtocol limit=10
```
**Expected:** Searches all 240 UTF-8 text files via trigram index + live-disk overlay, returning global `file_counts`, symbol-attributed `matches`, and `stats`.

### 8.6 MCP Tool 5 (Regex Variant) — `search_code` (JSON Arguments)
```bash
./cbi mcp-call search_code '{"query": "\\bui\\b", "regex": true, "limit": 10}'
```
**Expected:** Executes regex word-boundary search via JSON argument object.

### 8.7 MCP Tool 6 — `get_symbol_source`
```bash
./cbi mcp-call get_symbol_source CodeSearchService.search max_lines=25 offset=0
```
**Expected:** Returns bounded, line-numbered live source slice for `CodeSearchService.search`.

### 8.8 MCP Tool 7 — `get_symbol_context`
```bash
./cbi mcp-call get_symbol_context SearchCodeInput include_source=true
```
**Expected:** Returns compact structural hierarchy, callers/callees, freshness, and source for `SearchCodeInput`.

### 8.9 MCP Tool 8 — `get_symbol_references`
```bash
./cbi mcp-call get_symbol_references SearchCodeInput direction=both limit=15
```
**Expected:** Returns compact incoming/outgoing reference sites with 1-line previews.

### 8.10 MCP Tool 9 — `analyze_risk`
```bash
./cbi mcp-call analyze_risk SearchCodeInput IndexerEngine
```
**Expected:** Returns compact multi-target risk score, level, decision, factors, and blast-radius facts.

---

## 9. Interactive Shell, Watcher, MCP Server & Security Guards

### 9.1 Start Interactive `cbi>` Shell (`./cbi shell`)
```bash
./cbi shell
```
**Expected:** Opens the interactive `cbi>` REPL bound to `./codebase_indexer.db` and the active repo (type `help`, `db status`, `search TextSearchRepositoryProtocol`, or `exit`).

### 9.2 Start Live Repository Watcher (`./cbi watch`)
```bash
./cbi watch . --debounce-ms 300 --verbose
```
**Expected:** Emits `watch_started` JSON and incrementally re-indexes `.py` changes on save until stopped with `Ctrl-C`.

### 9.3 Test MCP `stdio` JSON-RPC Initialize Handshake (`./cbi serve`)
```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"manual-check","version":"0.2.0"}}}\n' | ./cbi serve --debug
```
**Expected:** Emits clean JSON-RPC `initialize` response on `stdout` (`"serverInfo": {"name": "codebase-indexer"}`) and debug logs on `stderr` only.

### 9.4 Verify Global `--db-path` Flag Placement
```bash
./cbi --db-path codebase_indexer.db db status
```
**Expected:** Resolves `--db-path` before the subcommand identically to trailing `--db-path`.

### 9.5 Verify Missing-DB Read-Only Guard Creates Zero Files
```bash
./cbi db status --db-path /tmp/nonexistent_cbi_manual_check.db
```
**Expected:** Fails with `"code": "database_not_found"` JSON error on `stderr` (exit code `1`) and does **not** create `/tmp/nonexistent_cbi_manual_check.db`.
