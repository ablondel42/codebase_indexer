# Manual Testing Guide (`./cbi`)

Copy-pasteable 2–3 line terminal checks for every `./cbi` bootstrapper mode, headless `IndexerEngine` query, grouped CLI subcommand (`src/cli/main.py`), interactive `cbi>` shell feature, security guard, daemon, and all 8 MCP tools (`src/mcp_server/`).

Run all commands from the repository root (`codebase_indexer`). Most inspection commands read `./codebase_indexer.db` created in Section 1.

---

## 1. Bootstrapper (`./cbi`), Dev Rebuild & Test Suite

### Show Quickstart Menu (`./cbi`)
```bash
./cbi
./cbi --version
./cbi --help | head -n 25
```
**Expected:** Prints the `./cbi` quickstart commands (`path/to/repo`, `demo`, `dev`, `check`, `tests`), version `cbi 0.2.0`, and the grouped subcommand list.

### Run Automated Linter, Typechecker & Test Suite (`./cbi check`, `./cbi tests`)
```bash
./cbi check
./cbi tests -k test_bootstrap
./cbi tests
```
**Expected:** `./cbi check` verifies Ruff + Pyright (`0 errors`) + Hexagonal AST gate; `./cbi tests` runs `ruff check src/ tests/` (`All checks passed!`) followed by `pytest -v`.

### Index Local Repo, Demo & Full Dev Rebuild (`./cbi demo`, `./cbi dev`)
```bash
./cbi . --no-cli
printf "db status\nexit\n" | ./cbi demo --cli
./cbi dev
```
**Expected:** First command indexes `.` into `./codebase_indexer.db` and exits; second indexes `.` (including `tests/`) and launches then exits `cbi>`; `./cbi dev` rebuilds `./codebase_indexer.db` from scratch with `--include-tests --force --lsp`.

### Index External Repository (`./cbi /path/to/repo`)
```bash
cp -r tests/fixtures/simple_package /tmp/manual_ext_repo
printf "files\nsymbols list\nexit\n" | ./cbi /tmp/manual_ext_repo --db-path /tmp/manual_ext.db
rm -rf /tmp/manual_ext_repo /tmp/manual_ext.db*
```
**Expected:** Indexes `/tmp/manual_ext_repo` into `/tmp/manual_ext.db`, opens `cbi>` showing its files and symbols, and cleans up `/tmp`.

---

## 2. Indexing (`cbi index`) & Headless `IndexerEngine`

### Incremental Index vs `--force` & `--include-tests`
```bash
./cbi index . --include-tests
./cbi index . --include-tests --force | head -n 20
```
**Expected:** First run skips unchanged files (`"files_unchanged"`); `--force` re-extracts all files across `src/{core,storage,parsing,lsp,graph,context,indexing,engine,mcp_server,cli}` and `tests/`.

### Optional Pyright LSP Index Enrichment (`--lsp`)
```bash
./cbi index tests/fixtures/real_workflow/cross_file_refs --lsp --db-path /tmp/manual_lsp.db
rm -f /tmp/manual_lsp.db*
```
**Expected:** Reports `"available": true` under `"lsp"` if `pyright-langserver` is in `PATH`, or records the error under `"lsp"` without failing AST indexing.

### Headless Python `IndexerEngine` Check
```bash
PYTHONPATH=src ./venv/bin/python -c 'from core import AppConfig; from engine import IndexerEngine, SymbolMembersQuery; eng = IndexerEngine(AppConfig(db_path="codebase_indexer.db")); print([m.name for m in eng.queries.get_symbol_members(SymbolMembersQuery(symbol="IndexerEngine", include_private=False))]); eng.close()'
```
**Expected:** Prints public members of `IndexerEngine` (`['config', 'db_path', 'queries', 'commands', 'lsp_pool', 'for_db', 'close']`) with zero stdout logging noise.

---

## 3. Database Inspection (`cbi db ...`)

### Database Status, Repos, Tables & Runs
```bash
./cbi db status
./cbi db repos
./cbi db tables
```
**Expected:** Emits indented JSON showing `"user_version": 2`, DB size, active repo, edge breakdown (`calls`, `imports`, `contains`, `scope_parent`, `inherits`, `references`), and row counts including `nodes_fts`.

### Index Run History (`cbi db runs`)
```bash
./cbi db runs --limit 5
./cbi db status | head -n 20
```
**Expected:** Prints recent `index_runs` entries with `"status": "completed"` and database status JSON.

### Guarded Read-Only SQL Queries (`cbi db sql`)
```bash
./cbi db sql "SELECT kind, COUNT(*) AS n FROM nodes GROUP BY kind ORDER BY n DESC"
./cbi db sql "SELECT name, qualified_name FROM nodes_fts WHERE nodes_fts MATCH 'IndexerEngine' LIMIT 5"
! ./cbi db sql "DELETE FROM nodes"
```
**Expected:** `SELECT` queries emit JSON row objects; `DELETE FROM nodes` is blocked with `"not authorized"` JSON error on `stderr` (exit code `1`, caught by `!`).

---

## 4. Files & Symbol Outlines (`cbi files`, `cbi outline`)

### List Indexed Files & Live Disk Freshness (`cbi files`)
```bash
./cbi files mcp_server
./cbi files --include-tests --limit 15
```
**Expected:** Emits JSON file entries under `src/mcp_server/` with line counts, symbol counts, `is_test`, and `"freshness": "fresh"`.

### Hierarchical File Symbol Outline (`cbi outline`)
```bash
./cbi outline src/engine/services/facade.py
./cbi outline src/mcp_server/services/registry.py --no-private --max-depth 2
```
**Expected:** Emits hierarchical JSON outline of classes, fields, methods, and constants with line spans, signatures, and `sym:...` IDs.

---

## 5. Symbol Search, Browse, Context & Source (`cbi symbols`, `cbi source`)

### FTS5 Symbol Search (`cbi symbols search`)
```bash
./cbi symbols search ListFilesInput
./cbi symbols search build_context --kind method --file src/context/
```
**Expected:** Prints matching symbols with full `sym:...` IDs, kinds, `start_line`/`end_line` spans, and typed signatures.

### Paginated Symbol Browser (`cbi symbols list`)
```bash
./cbi symbols list --kind class --file src/mcp_server/ --limit 15
./cbi symbols list --kind method --file src/engine/ --limit 10 --offset 0
```
**Expected:** Lists classes/methods under `src/mcp_server/` and `src/engine/` with pagination and untruncated symbol IDs.

### Full Symbol Context & Source Snippet (`cbi symbols show`, `cbi source`)
```bash
./cbi symbols show IndexerEngine --source
./cbi source SymbolContextBuilder.build_context --max-lines 25 --offset 0
```
**Expected:** Shows focus symbol metadata, parent, children, callers/callees, freshness, and line-numbered source slices with pagination hints.

---

## 6. References & Risk Analysis (`cbi refs`, `cbi risk`)

### Incoming & Outgoing References (`cbi refs`)
```bash
./cbi refs ListFilesInput --direction both --limit 20
./cbi refs SymbolContextBuilder.build_context --direction incoming --refresh
```
**Expected:** Lists callers/callees/imports/bases with `file:line` and 1-line trimmed source previews (`--refresh` invokes Pyright LSP when available).

### Single & Multi-Target Change Risk (`cbi risk`)
```bash
./cbi risk ListFilesInput
./cbi risk ListFilesInput IndexerEngine | head -n 25
```
**Expected:** Emits JSON risk score (`0–100`), decision (`safe_enough` / `review_required` / `high_risk`), target breakdown, and triggered issue codes.

---

## 7. Interactive Shell (`cbi shell`)

### Interactive REPL Session (`./cbi shell`)
```bash
printf "help\ndb status\ndb sql SELECT kind, COUNT(*) AS n FROM edges GROUP BY kind\noutline src/mcp_server/models/inputs.py\nmcp-call get_symbol_context ListFilesInput\nexit\n" | ./cbi shell
```
**Expected:** Enters `cbi>`, executes all commands with auto-bound `--db-path` and `--repo`, and exits cleanly (or run `./cbi shell` interactively without `printf`).

---

## 8. All 8 MCP Tools via `cbi mcp-call`

### 1. `get_repo_overview` & 2. `list_files`
```bash
./cbi mcp-call get_repo_overview
./cbi mcp-call list_files src/mcp_server limit=10 include_tests=false
```
**Expected:** Returns repo stats, recent runs, and paginated tracked files with payload byte/token metrics.

### 3. `get_file_outline` & 4. `search_symbols`
```bash
./cbi mcp-call get_file_outline src/mcp_server/models/inputs.py max_depth=2
./cbi mcp-call search_symbols ListFilesInput limit=5
```
**Expected:** Returns compact JSON file outline and ranked symbol matches (`"ok": true`).

### 5. `get_symbol_source` & 6. `get_symbol_context`
```bash
./cbi mcp-call get_symbol_source SymbolContextBuilder.build_context max_lines=20 offset=0
./cbi mcp-call get_symbol_context ListFilesInput include_source=true
```
**Expected:** Returns line-numbered source snippet and slim symbol context; JSON form `'{"symbol": "ListFilesInput"}'` also works identically.

### 7. `get_symbol_references` & 8. `analyze_risk`
```bash
./cbi mcp-call get_symbol_references ListFilesInput direction=both limit=15
./cbi mcp-call analyze_risk ListFilesInput IndexerEngine
```
**Expected:** Returns paginated reference edges with 1-line previews and multi-target deterministic risk assessment.

---

## 9. Daemons, Global Flags & Security Checks

### Live Watch Mode (`cbi watch`)
```bash
cp -r tests/fixtures/simple_package /tmp/manual_watch_repo
./cbi watch /tmp/manual_watch_repo --db-path /tmp/manual_watch.db --debounce-ms 200 --verbose &
WPID=$!; sleep 1; printf "\ndef added_live():\n    return 42\n" >> /tmp/manual_watch_repo/services.py; sleep 1.5; kill "$WPID"; wait "$WPID" 2>/dev/null || true
./cbi symbols search added_live --db-path /tmp/manual_watch.db && rm -rf /tmp/manual_watch_repo /tmp/manual_watch.db*
```
**Expected:** Watch detects the appended function and incrementally indexes `added_live` into `/tmp/manual_watch.db`.

### MCP `stdio` Server (`cbi serve` / `python -m cli.main serve`)
```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"check","version":"0.2"}}}\n' | ./cbi serve --debug
./venv/bin/python -c 'import asyncio, mcp, mcp.client.stdio as s; exec("async def f():\n async with s.stdio_client(mcp.StdioServerParameters(command=\"./cbi\",args=[\"serve\"])) as (r,w):\n  async with mcp.ClientSession(r,w) as c: await c.initialize(); print([t.name for t in (await c.list_tools()).tools])\nasyncio.run(f())")'
```
**Expected:** First line prints the `initialize` JSON-RPC response on `stdout` (and debug startup log on `stderr`); second line connects as a live MCP client and prints all 8 tool names.

### Global Flags & Missing-DB Read-Only Guard
```bash
./cbi --db-path codebase_indexer.db db tables
! ./cbi db status --db-path /tmp/nonexistent_cbi_check.db && test ! -e /tmp/nonexistent_cbi_check.db && echo "OK: 0 files created"
```
**Expected:** Global `--db-path` works before or after subcommands; missing `--db-path` fails with `"database_not_found"` JSON error on `stderr` and creates zero files on disk.
