# Codebase Indexer (`codebase-indexer` v0.2.0)

Local repository intelligence for safer, token-lean AI-assisted code planning and UI code exploration. Indexes Python codebases into a local SQLite graph (WAL + FTS5) and exposes structural outlines, UI-ready raw/numbered symbol snippets, neighborhood subgraphs, AST/LSP references, and deterministic change-risk analysis over a headless Python `IndexerEngine`, an 8-tool MCP server, and the grouped `cbi` CLI.

---

## Hexagonal Service Architecture (`src/`)

All 10 domain packages live directly under `src/` using a strict **Hexagonal (Ports & Adapters) + `<domain>/{services,models,utils}`** layout (`src/core/{ports,models,utils}`):

```text
Driving Adapters (Thin Consumers)
  ├── src/cli/{services,models,utils} + main.py     Grouped `cbi` CLI & interactive `cbi>` REPL
  └── src/mcp_server/{services,models,utils}        8 token-lean MCP tools over stdio

Application Layer (Headless & UI-Ready)
  └── src/engine/{services,models,utils}            IndexerEngine = QueryEngine + CommandEngine

Domain Read & Write Layers (CQRS)
  ├── src/context/{services,models,utils}           Outlines, SourceReader, SymbolContextBuilder
  ├── src/graph/{services,models,utils}             GraphNavigator, SubgraphSlice, RiskAnalyzer
  ├── src/indexing/{services,models,utils}          BatchIndexer, IncrementalIndexer, RepoWatcher
  └── src/parsing/{services,models,utils}           RepoScanner, AstExtractionPipeline, SymbolLinker

Driven Infrastructure Adapters
  ├── src/storage/{services,models,utils}           100% of SQLAlchemy Core + SQLite v2 (WAL/FTS5)
  └── src/lsp/{services,models,utils}               Pyright JSON-RPC transport, pool & enricher

Innermost Core (Zero Sibling Dependencies)
  └── src/core/{ports,models,utils}                 FrozenModel entities, IndexerError, UoW/Repo Protocols
```

### Core Capabilities by Package

1. **Foundation & Ports (`src/core/`)**
   - Immutable Pydantic v2 entities (`FrozenModel` / `MutableModel` with `extra="forbid"`), structured `Range` and `payload` fields on `SymbolNode` and `Edge`, `IndexerError` exception hierarchy, `"indexer"` stderr-only logging, and Hexagonal `UnitOfWorkProtocol` / repository / LSP ports.

2. **Persistence Adapter (`src/storage/`)**
   - Isolates **100%** of SQLAlchemy 2.x Core and `sqlite3` operations (`SqlUnitOfWorkFactory`, `SqlUnitOfWork`, `ConnectionFactory`, `SchemaMigrator`) over SQLite schema `user_version = 4` (`repos`, `files`, `text_files`, `nodes`, `edges`, `index_runs`, `reference_refresh`, FTS5 `nodes_fts`, and trigram FTS5 `text_fts`).
   - Enforces WAL mode, `foreign_keys = ON`, `busy_timeout = 5000`, read-only `query_only = ON` sessions, and SQLite's `SELECT`-only authorizer for `cbi db sql`.

3. **Static Analysis & Intra-Repo Linker (`src/parsing/`)**
   - `RepoScanner` walks repositories safely (`followlinks=False`, symlink/escape rejection, `ignored_dirs` globs, `--include-tests`, 2 MiB file cap).
   - `AstExtractionPipeline` and `DeclarationWalker` extract modules, classes, and functions/methods (sync & async, nested/local), plus `imports`, `inherits`, and linkable `calls` edges.
   - `SymbolLinker` resolves imports (`__init__.py` re-exports, `src.` layouts), base-class hierarchies, and calls in memory, resetting broken targets back to unresolved placeholders when files are deleted or refactored.

4. **Graph, Risk, Outlines & Snippets (`src/graph/`, `src/context/`)**
   - `GraphNavigator` and `RiskAnalyzer` compute bounded neighborhood `SubgraphSlice`s, paginated `SymbolReferencesReport`s with 1-line source previews, and deterministic `0-100` `RiskAssessment`s (`safe_enough`, `review_required`, `high_risk`).
   - `OutlineBuilder`, `SourceReader`, and `SymbolContextBuilder` produce hierarchical `FileOutline`s, single-pass batched `SymbolSnippet`s (carrying both clean `raw_source` for syntax highlighters and `numbered_source` for CLI/MCP), and bounded `SymbolContext`s.

5. **Batch, Incremental & Watch Orchestration (`src/indexing/`, `src/lsp/`)**
   - `BatchIndexer` executes hash-diffed repository indexing runs with optional `ReferenceEnricher` + `LspSessionPool` (`pyright-langserver --stdio`) enrichment.
   - `IncrementalIndexer` and `RepoWatcher` isolate per-file updates in SQLite savepoints, link once per debounced batch, preserve surviving callers' AST edges across file deletions for automatic re-linking, and emit typed lifecycle events via callbacks.

6. **Headless `IndexerEngine`, MCP Server & `cbi` CLI (`src/engine/`, `src/mcp_server/`, `src/cli/`)**
   - `IndexerEngine` exposes `engine.queries` and `engine.commands`, with `engine.for_db(db_path)` sharing warm Pyright LSP sessions across databases with zero global mutable state.
   - `McpServerRunner` / `McpToolRegistry` expose 8 compact MCP tools over `stdio`, and `CliApplication` (`python -m cli.main` / `./cbi`) emits indented JSON to `stdout`.

---

## Headless Python Usage (`IndexerEngine`)

```python
from core import AppConfig
from engine import (
    IndexerEngine,
    OutlineQuery,
    SubgraphQuery,
    SymbolMembersQuery,
    SymbolSnippetsQuery,
)
from indexing import BatchIndexOptions

config = AppConfig(db_path="codebase_indexer.db", include_tests=True)

with IndexerEngine(config) as engine:
    # 1. Index a repository
    run = engine.commands.index_repository(".", BatchIndexOptions(include_tests=True))

    # 2. Resolve symbols, class/module members, and file outlines
    sym = engine.queries.get_symbol("IndexerEngine")
    members = engine.queries.get_symbol_members(
        SymbolMembersQuery(symbol=sym.id, include_private=False)
    )
    outline = engine.queries.get_file_outline(
        OutlineQuery(file_path="src/engine/services/facade.py")
    )

    # 3. Fetch UI-ready snippets (clean raw_source + CLI numbered_source) & BFS subgraphs
    snippets = engine.queries.get_symbol_snippets(
        SymbolSnippetsQuery(symbols=[sym.id], padding_lines=2)
    )
    subgraph = engine.queries.get_symbol_subgraph(
        SubgraphQuery(symbols=[sym.id], depth=1, max_nodes=40)
    )
```

---

## Quickstart (`./cbi`)

Use the root `./cbi` bootstrap script (automatically sets up `venv/` on first run, falling back cleanly to Python 3.11+ with `PYTHONPATH=src` when offline):

```bash
# Show the quickstart commands
./cbi

# Index any repository into ./codebase_indexer.db and open the interactive `cbi>` shell (--cli is optional)
./cbi path/to/repo --cli
./cbi path/to/repo --no-cli   # index only without entering the interactive shell

# Demo: index this repository (including tests) and start the interactive `cbi>` shell
./cbi demo --cli

# Dev rebuild: recreate codebase_indexer.db from scratch with tests + Pyright LSP enrichment
./cbi dev

# Fast Ruff + Pyright + Hexagonal AST architecture gate (add --tests for pytest)
./cbi check

# Run the full test suite (ruff check + pytest)
./cbi tests
```

Or install manually in editable mode (requires Python 3.11+):

```bash
pip install -e ".[dev]"
```

Run standalone CLI commands directly (`./cbi <subcommand>`, `cbi <subcommand>`, or `PYTHONPATH=src python -m cli.main <subcommand>`), or follow the copy-paste verification checklist in [MANUAL_TESTING.md](file:///Users/arnaud/dev/codebase_indexer/MANUAL_TESTING.md):

```bash
./cbi index .
./cbi shell
```

Inspect database health, schema version, table counts, and latest run:

```bash
./cbi db status
```

Inspect a file outline (with symbol IDs, line ranges, kinds, and signatures):

```bash
./cbi outline src/engine/services/facade.py
```

Search symbols via FTS5:

```bash
./cbi symbols search IndexerEngine
```

Read bounded, line-numbered source for a symbol (by `sym:...` ID, qualified name, `file.py:symbol`, or unique name):

```bash
./cbi source IndexerEngine --max-lines 80
```

Run a guarded read-only SQL query against the index:

```bash
./cbi db sql "SELECT kind, COUNT(*) AS count FROM nodes GROUP BY kind ORDER BY count DESC"
```

Invoke any of the 8 MCP tools locally and inspect payload byte/token metrics:

```bash
./cbi mcp-call get_repo_overview
./cbi mcp-call search_symbols '{"query": "SymbolLinker", "limit": 5}'
```

---

## CLI Reference (`cbi`)

All CLI commands emit indented JSON objects to `stdout` (and structured JSON error objects to `stderr`). The global `--db-path PATH` flag works either before or after subcommands (e.g., `cbi --db-path custom.db db status`).

| Command | Purpose |
| --- | --- |
| `cbi index [repo_path] [--include-tests] [--lsp] [--force]` | Scan repo, extract changed files atomically, prune deleted files, link edges, and optionally enrich LSP references |
| `cbi db status [--repo]` | Show DB size, `user_version`, repo summary, table row counts, edge kinds, LSP coverage, and latest run |
| `cbi db repos` | List indexed repositories |
| `cbi db tables` | List SQLite tables and exact row counts |
| `cbi db runs [--repo] [--limit 20]` | List recent `index_runs` entries |
| `cbi db sql <query> [--limit 100]` | Execute a read-only SQL query guarded by SQLite's SELECT-only authorizer |
| `cbi files [pattern] [--repo] [--include-tests] [--limit 50]` | List tracked files with line count, symbol count, and live disk freshness (`fresh`/`modified`/`deleted`) |
| `cbi outline <file_path> [--repo] [--no-private] [--max-depth 3]` | Show hierarchical symbol outline for a file |
| `cbi symbols search <query> [--repo] [--kind] [--file] [--limit 20]` | Search symbols using FTS5 + ranking |
| `cbi symbols list [--repo] [--kind] [--file] [--limit 50] [--offset 0]` | Browse symbols with pagination |
| `cbi symbols show <id_or_name> [--repo] [--source]` | Inspect full symbol context (+ optional source snippet) |
| `cbi source <id_or_name> [--repo] [--max-lines 120] [--offset 0]` | Print line-numbered symbol source slice |
| `cbi refs <id_or_name> [--repo] [--direction incoming\|outgoing\|both] [--refresh] [--limit 50]` | List calls/references with `file:line` and 1-line previews (optionally refreshing via Pyright LSP) |
| `cbi risk <id_or_names...> [--repo]` | Compute risk score, decision, targets, facts, and issue codes |
| `cbi shell [--repo]` | Start interactive `cbi>` REPL bound to the database and active repository |
| `cbi watch [repo_path] [--debounce-ms 500] [--include-tests] [--verbose]` | Watch repository for changes and incrementally update SQLite |
| `cbi serve [--debug]` | Start the MCP server on `stdio` |
| `cbi mcp-call <tool_name> [json_args]` | Execute one of the 8 MCP tools locally and print payload size + `~bytes/4` token estimate |

---

## MCP Server Setup (`stdio`)

Index your repository first, then configure your MCP client to launch `cbi serve` pointing to the database file:

```json
{
  "mcpServers": {
    "codebase-indexer": {
      "command": "cbi",
      "args": [
        "serve",
        "--db-path",
        "/absolute/path/to/codebase_indexer.db"
      ]
    }
  }
}
```

### Exposed MCP Tools

1. `get_repo_overview` — repository metadata, symbol/edge counts, top directories, and recent runs.
2. `list_files` — paginated tracked files with optional `path_prefix` and `include_tests`.
3. `get_file_outline` — hierarchical symbol skeleton, line spans, signatures, imports, and disk freshness.
4. `search_symbols` — FTS5 search with exact/prefix ranking and `kind` / `file_path` filters.
5. `get_symbol_source` — bounded, line-numbered source slicing with disk freshness verification.
6. `get_symbol_context` — bounded symbol hierarchy, callers/callees/imports/inherits buckets, and summaries.
7. `get_symbol_references` — paginated LSP references and AST dependency sites with 1-line previews (`refresh_lsp=True` optional).
8. `analyze_risk` — deterministic change-impact and risk assessment for 1 to 50 target symbols.
