# Repo Context MCP (`repo-context-mcp` v0.2.0)

Local repository intelligence for safer, token-lean AI-assisted code planning. Indexes Python codebases into a local SQLite graph (WAL + FTS5) and exposes structural outlines, bounded source reading, AST/LSP references, and deterministic change-risk analysis over an 8-tool MCP server and the grouped `rc` CLI.

---

## Architecture Overview

```text
Local Repo
  -> Scanner (path confinement, symlink rejection, SHA-256, line counts)
  -> AST Extraction + Calls / Constants / Attributes / Type Aliases + Intra-Repo Linker
  -> SQLite v2 (WAL mode, foreign keys, FTS5 symbol search, index_runs)
  -> Context / Outline / Source / Pyright LSP / Risk Services
  -> 8 Token-Lean MCP Tools & `rc` CLI
```

### Core Layers

1. **Scanner & Security (`repo_context.parsing`)**
   - Walks the repository with `followlinks=False`, rejecting symlinks and path escapes via `ensure_safe_repo_file`.
   - Respects `ignored_dirs` (including globs such as `*.egg-info`), `test_dir_names` (`--include-tests`), and `max_file_size_bytes` (2 MiB default).
   - Computes SHA-256 hashes, byte sizes, line counts, module paths, and test flags.

2. **AST Extraction & Intra-Repo Linker (`repo_context.parsing`)**
   - Extracts `module`, `class`, `function`, `async_function`, `method`, `async_method`, `local_function`, `local_async_function`, `constant`, `attribute`, and `type_alias` nodes with compact signatures, 1-based line spans (`start_line`, `end_line`), and UTF-8-accurate ranges.
   - Emits `contains`, `scope_parent`, `imports`, `inherits`, and `calls` edges.
   - Resolves intra-repo links (`resolve_repo_edges`) across imports (including aliases, `src.` layouts, and `__init__.py` re-exports), class inheritance hierarchies, and `self`/`cls`/`super()`/module/local calls.

3. **SQLite Storage v2 (`repo_context.storage`)**
   - Schema `user_version = 2` with `repos`, `files`, `nodes`, `edges`, `index_runs`, `reference_refresh`, and `nodes_fts` (FTS5 with `unicode61` tokenizer and automatic insert/update/delete triggers).
   - Writable connections enable WAL mode, `foreign_keys = ON`, and `busy_timeout = 5000`.
   - Read-only inspection and query commands use `read_only=True` (`mode=ro`, `query_only=ON`); `rc db sql` additionally enforces a SQLite authorizer allowing only `SELECT`, `READ`, `FUNCTION`, and `PRAGMA` actions.

4. **Context, Outlines, Source Reader & Watch Mode (`repo_context.context`, `repo_context.indexing`)**
   - `build_file_outline` produces hierarchical symbol trees with deduplicated imports and live disk freshness checks.
   - `read_symbol_source` returns bounded, line-numbered slices with pagination (`max_lines`, `offset`) and SHA-256 freshness verification.
   - `build_symbol_context` assembles bounded structural/lexical hierarchy, balanced semantic relationships, and freshness/confidence summaries.
   - `watch_repo` uses `watchdog` with debouncing to incrementally reindex changed files, clean up deleted files, re-link edges, and invalidate stale reference caches.

5. **LSP Reference Enrichment & Risk Engine (`repo_context.lsp`, `repo_context.graph`)**
   - `PyrightLspClient` and `LspSessionPool` run `pyright-langserver --stdio` with bounded JSON-RPC framing, UTF-16 <-> codepoint offset mapping, and clean subprocess cleanup.
   - `analyze_symbol_risk` / `analyze_target_set_risk` evaluate reference fanout (using LSP `references` when refreshed, with transparent AST `calls`/`imports`/`inherits` fallback), cross-file/cross-module impact, public-surface exposure, inheritance involvement, disk staleness, and low-confidence edges to compute a `0-100` risk score and decision (`safe_enough`, `review_required`, `high_risk`).

6. **Token-Lean MCP Server & `rc` CLI (`repo_context.mcp`, `repo_context.cli`)**
   - Exposes 8 compact MCP tools over `stdio` without polluting `stdout` (all logs are directed strictly to `stderr`).
   - Provides the grouped `rc` CLI (`--db-path`, `--json` supported on root or subcommands).

---

## Quickstart

Use the root `./rc` bootstrap script (automatically sets up `.venv` on first run, falling back cleanly to Python 3.11+ with `PYTHONPATH=src` when offline):

```bash
# Show the quickstart commands
./rc

# Index any repository into ./repo_context.db and open the interactive `rc>` shell (--cli is optional)
./rc path/to/repo --cli
./rc path/to/repo --no-cli   # index only without entering the interactive shell

# Demo: index this repository (including tests) and start the interactive `rc>` shell
./rc demo --cli

# Run the full test suite (ruff check + pytest)
./rc tests
```

Or install manually in editable mode (requires Python 3.11+):

```bash
pip install -e ".[dev]"
```

Run standalone CLI commands directly (`./rc <subcommand>` or `rc <subcommand>`), or follow the copy-paste verification checklist in [MANUAL_TESTING.md](file:///Users/arnaud/dev/codebase_indexer/MANUAL_TESTING.md):

```bash
rc index .
rc shell
```

Inspect database health, schema version, table counts, and latest run:

```bash
rc db status
```

Inspect a file outline tree (with symbol IDs, line ranges, kinds, and signatures):

```bash
rc outline src/repo_context/cli/main.py
```

Search symbols via FTS5:

```bash
rc symbols search build_symbol_context
```

Read bounded, line-numbered source for a symbol (by `sym:...` ID, qualified name, or unique name):

```bash
rc source build_symbol_context --max-lines 80
```

Run a guarded read-only SQL query against the index:

```bash
rc db sql "SELECT kind, COUNT(*) AS count FROM nodes GROUP BY kind ORDER BY count DESC"
```

Invoke any of the 8 MCP tools locally and inspect payload byte/token metrics:

```bash
rc mcp-call get_repo_overview
rc mcp-call search_symbols '{"query": "resolve_repo_edges", "limit": 5}'
```

---

## CLI Reference (`rc`)

Global flags `--db-path PATH` and `--json` work either before or after subcommands (e.g., `rc --db-path custom.db db status --json`).

| Command | Purpose |
| --- | --- |
| `rc index [repo_path] [--include-tests] [--lsp] [--force]` | Scan repo, extract changed files atomically, prune deleted files, link edges, and optionally enrich LSP references |
| `rc db status [--repo]` | Show DB size, `user_version`, repo summary, table row counts, edge kinds, LSP coverage, and latest run |
| `rc db repos` | List indexed repositories |
| `rc db tables` | List SQLite tables and exact row counts |
| `rc db runs [--repo] [--limit 20]` | List recent `index_runs` entries |
| `rc db sql <query> [--limit 100]` | Execute a read-only SQL query guarded by SQLite's SELECT-only authorizer |
| `rc files [pattern] [--repo] [--include-tests] [--limit 50]` | List tracked files with line count, symbol count, and live disk freshness (`fresh`/`modified`/`deleted`) |
| `rc outline <file_path> [--repo] [--no-private] [--max-depth 3]` | Print indented symbol outline tree for a file |
| `rc symbols search <query> [--repo] [--kind] [--file] [--limit 20]` | Search symbols using FTS5 + ranking |
| `rc symbols list [--repo] [--kind] [--file] [--limit 50] [--offset 0]` | Browse symbols with pagination |
| `rc symbols show <id_or_name> [--repo] [--source]` | Inspect full symbol context (+ optional source snippet) |
| `rc source <id_or_name> [--repo] [--max-lines 120] [--offset 0]` | Print line-numbered symbol source slice |
| `rc refs <id_or_name> [--repo] [--direction incoming\|outgoing\|both] [--refresh] [--limit 50]` | List calls/references with `file:line` and 1-line previews (optionally refreshing via Pyright LSP) |
| `rc risk <id_or_names...> [--repo]` | Compute risk score, decision, targets, facts, and issue codes |
| `rc shell [--repo]` | Start interactive `rc>` REPL bound to the database and active repository |
| `rc watch [repo_path] [--debounce-ms 500] [--include-tests] [--verbose]` | Watch repository for changes and incrementally update SQLite |
| `rc serve [--debug]` | Start the FastMCP server on `stdio` |
| `rc mcp-call <tool_name> [json_args]` | Execute one of the 8 MCP tools locally and print payload size + `~bytes/4` token estimate |

---

## MCP Server Setup (`stdio`)

Index your repository first, then configure your MCP client to launch `rc serve` pointing to the database file:

```json
{
  "mcpServers": {
    "repo-context": {
      "command": "rc",
      "args": [
        "serve",
        "--db-path",
        "/absolute/path/to/repo_context.db"
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
