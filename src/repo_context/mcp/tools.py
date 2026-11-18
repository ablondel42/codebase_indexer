"""Token-lean MCP tool handlers executing blocking I/O in worker threads."""

import asyncio
from collections import Counter
import json
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from sqlalchemy import func, or_, select
from sqlalchemy.engine import Connection

from repo_context.config import get_config
from repo_context.context import (
    build_file_outline,
    build_symbol_context,
    read_symbol_source,
)
from repo_context.context.helpers import format_line_span
from repo_context.context.source_reader import _read_verified_file_text
from repo_context.exceptions import (
    DatabaseError,
    ParseError,
    PathSecurityError,
    RepoContextError,
    SymbolNotFoundError,
)
from repo_context.graph import (
    analyze_target_set_risk as engine_analyze_target_set_risk,
    build_reference_stats,
    count_symbol_reference_edges,
    get_files_by_ids,
    get_repo_graph_stats,
    get_symbols_by_ids,
    list_symbol_reference_edges,
    search_symbols_fts,
)
from repo_context.lsp import LspSessionPool
from repo_context.lsp.references import enrich_references_for_symbol
from repo_context.mcp.adapters import (
    adapt_context,
    adapt_edge,
    adapt_node,
    adapt_outline,
    adapt_risk_result,
)
from repo_context.mcp.errors import (
    ERROR_AMBIGUOUS_SYMBOL,
    map_exception_to_error_result,
    success_result,
)
from repo_context.mcp.schemas import (
    AnalyzeRiskInput,
    DirectionLiteral,
    GetFileOutlineInput,
    GetRepoOverviewInput,
    GetSymbolContextInput,
    GetSymbolReferencesInput,
    GetSymbolSourceInput,
    ListFilesInput,
    SearchSymbolsInput,
    SymbolKindLiteral,
)
from repo_context.parsing.pathing import ensure_safe_repo_file
from repo_context.storage import (
    connection_scope,
    files_table,
    get_file_by_id,
    get_node_by_id,
    get_repo_by_id,
    initialize_database,
    list_index_runs,
    nodes_table,
    resolve_repo_id,
)
from repo_context.storage.nodes import row_to_node

_DEFAULT_LSP_POOL = LspSessionPool()
_MAX_PREVIEW_CHARS = 160


def close_default_lsp_pool() -> None:
    """Close any warm LSP sessions held in the module-level fallback pool."""
    _DEFAULT_LSP_POOL.close_all()


def _resolve_db_path(db_path: str | Path | None) -> Path:
    """Resolve explicit or default configured SQLite database path."""
    if db_path is not None:
        return Path(db_path)
    return get_config().db_path


def _serialize_result(payload: dict[str, Any]) -> str:
    """Serialize MCP response dictionary to compact JSON."""
    return json.dumps(payload, separators=(",", ":"))


def _escape_like(value: str) -> str:
    """Escape SQLite LIKE metacharacters for ``ESCAPE '\\'``."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_path_prefix(path_prefix: str) -> str:
    """Strip leading ``./`` segments and trailing slashes without mangling dot-directories."""
    clean = path_prefix.strip()
    while clean.startswith("./"):
        clean = clean.removeprefix("./")
    return clean.rstrip("/")


def _format_ambiguous_candidates(conn: Connection, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build compact disambiguation candidate entries for ambiguous symbol lookups."""
    file_ids = [c.get("file_id") for c in candidates if c.get("file_id")]
    files_by_id = get_files_by_ids(conn, file_ids)
    items: list[dict[str, Any]] = []
    for cand in candidates[:20]:
        finfo = files_by_id.get(cand.get("file_id", ""))
        entry: dict[str, Any] = {
            "id": cand["id"],
            "kind": cand["kind"],
            "qualified_name": cand["qualified_name"],
        }
        if finfo and finfo.get("file_path"):
            entry["file_path"] = finfo["file_path"]
        lines = format_line_span(cand)
        if lines:
            entry["lines"] = lines
        items.append(entry)
    return items


def _resolve_symbol_target(
    conn: Connection,
    repo_id: str,
    symbol_ref: str,
) -> dict[str, Any]:
    """Resolve a symbol by ID, exact qualified name, exact short name, or unique suffix."""
    if symbol_ref.startswith("sym:"):
        node = get_node_by_id(conn, symbol_ref)
        if node is None or node["repo_id"] != repo_id:
            raise SymbolNotFoundError(
                f"Symbol not found: {symbol_ref}",
                context={"symbol": symbol_ref, "repo_id": repo_id},
            )
        return node

    exact_qname_rows = (
        conn.execute(
            select(nodes_table)
            .where(
                nodes_table.c.repo_id == repo_id,
                nodes_table.c.qualified_name == symbol_ref,
            )
            .order_by(nodes_table.c.kind, nodes_table.c.id)
        )
        .mappings()
        .all()
    )
    if len(exact_qname_rows) == 1:
        return row_to_node(exact_qname_rows[0])
    if len(exact_qname_rows) > 1:
        candidates = [row_to_node(r) for r in exact_qname_rows]
        raise RepoContextError(
            f"Ambiguous symbol '{symbol_ref}' ({len(candidates)} matches).",
            error_code=ERROR_AMBIGUOUS_SYMBOL,
            context={"candidates": _format_ambiguous_candidates(conn, candidates)},
        )

    exact_name_rows = (
        conn.execute(
            select(nodes_table)
            .where(
                nodes_table.c.repo_id == repo_id,
                nodes_table.c.name == symbol_ref,
            )
            .order_by(nodes_table.c.kind, nodes_table.c.qualified_name, nodes_table.c.id)
        )
        .mappings()
        .all()
    )
    if len(exact_name_rows) == 1:
        return row_to_node(exact_name_rows[0])
    if len(exact_name_rows) > 1:
        candidates = [row_to_node(r) for r in exact_name_rows]
        raise RepoContextError(
            f"Ambiguous symbol '{symbol_ref}' ({len(candidates)} matches).",
            error_code=ERROR_AMBIGUOUS_SYMBOL,
            context={"candidates": _format_ambiguous_candidates(conn, candidates)},
        )

    suffix_pattern = f"%.{_escape_like(symbol_ref)}"
    suffix_rows = (
        conn.execute(
            select(nodes_table)
            .where(
                nodes_table.c.repo_id == repo_id,
                nodes_table.c.qualified_name.like(suffix_pattern, escape="\\"),
            )
            .order_by(nodes_table.c.kind, nodes_table.c.qualified_name, nodes_table.c.id)
            .limit(25)
        )
        .mappings()
        .all()
    )
    if len(suffix_rows) == 1:
        return row_to_node(suffix_rows[0])
    if len(suffix_rows) > 1:
        candidates = [row_to_node(r) for r in suffix_rows]
        raise RepoContextError(
            f"Ambiguous symbol '{symbol_ref}' ({len(candidates)} matches).",
            error_code=ERROR_AMBIGUOUS_SYMBOL,
            context={"candidates": _format_ambiguous_candidates(conn, candidates)},
        )

    raise SymbolNotFoundError(
        f"Symbol not found: {symbol_ref}",
        context={"symbol": symbol_ref, "repo_id": repo_id},
    )


def _sync_get_repo_overview(
    repo_id: str | None,
    db_path: str | Path | None,
) -> dict[str, Any]:
    """Synchronous implementation for ``get_repo_overview``."""
    inp = GetRepoOverviewInput(repo_id=repo_id)
    resolved_db = _resolve_db_path(db_path)

    with connection_scope(resolved_db, read_only=True) as conn:
        initialize_database(conn)
        resolved_repo_id = resolve_repo_id(conn, inp.repo_id)
        repo = get_repo_by_id(conn, resolved_repo_id)
        if repo is None:
            raise DatabaseError(
                f"Repository not found: {resolved_repo_id}",
                error_code="repo_not_found",
            )

        stats = get_repo_graph_stats(conn, resolved_repo_id)
        file_rows = (
            conn.execute(
                select(files_table.c.file_path, files_table.c.is_test)
                .where(files_table.c.repo_id == resolved_repo_id)
                .order_by(files_table.c.file_path)
            )
            .mappings()
            .all()
        )

        top_dir_counts: Counter[str] = Counter()
        for row in file_rows:
            if row["is_test"]:
                continue
            rel = row["file_path"]
            top_dir = rel.split("/", 1)[0] if "/" in rel else "."
            top_dir_counts[top_dir] += 1

        runs_raw = list_index_runs(conn, resolved_repo_id, limit=3)
        recent_runs = [
            {
                k: v
                for k, v in {
                    "id": r["id"],
                    "run_type": r["run_type"],
                    "status": r["status"],
                    "files_scanned": r["files_scanned"],
                    "nodes_extracted": r["nodes_extracted"],
                    "edges_extracted": r["edges_extracted"],
                    "completed_at": r["completed_at"],
                }.items()
                if v is not None
            }
            for r in runs_raw
        ]

        default_lang = (
            repo.default_language.value
            if hasattr(repo.default_language, "value")
            else str(repo.default_language)
        )

        return success_result(
            {
                "repo": {
                    "id": repo.id,
                    "name": repo.name,
                    "root_path": repo.root_path,
                    "language": default_lang,
                    "last_indexed_at": repo.last_indexed_at,
                },
                "stats": {
                    "file_count": stats["file_count"],
                    "test_file_count": stats["test_file_count"],
                    "non_test_file_count": stats["file_count"] - stats["test_file_count"],
                    "total_lines": stats["total_lines"],
                    "node_count": stats["node_count"],
                    "edge_count": stats["edge_count"],
                    "kind_counts": stats["kind_counts"],
                    "edge_kind_counts": stats["edge_kind_counts"],
                },
                "top_directories": [
                    {"path": path, "file_count": count}
                    for path, count in top_dir_counts.most_common(15)
                ],
                "recent_runs": recent_runs,
            }
        )


def _sync_list_files(
    repo_id: str | None,
    path_prefix: str | None,
    include_tests: bool,
    limit: int,
    offset: int,
    db_path: str | Path | None,
) -> dict[str, Any]:
    """Synchronous implementation for ``list_files``."""
    inp = ListFilesInput(
        repo_id=repo_id,
        path_prefix=path_prefix,
        include_tests=include_tests,
        limit=limit,
        offset=offset,
    )
    resolved_db = _resolve_db_path(db_path)

    with connection_scope(resolved_db, read_only=True) as conn:
        initialize_database(conn)
        resolved_repo_id = resolve_repo_id(conn, inp.repo_id)

        filters: list[Any] = [files_table.c.repo_id == resolved_repo_id]
        if not inp.include_tests:
            filters.append(files_table.c.is_test.is_(False))

        if inp.path_prefix is not None:
            clean_prefix = _normalize_path_prefix(inp.path_prefix)
            if clean_prefix:
                escaped = _escape_like(clean_prefix)
                filters.append(
                    or_(
                        files_table.c.file_path == clean_prefix,
                        files_table.c.file_path.like(f"{escaped}/%", escape="\\"),
                    )
                )

        total_count = int(
            conn.execute(select(func.count()).select_from(files_table).where(*filters)).scalar_one()
        )

        rows = (
            conn.execute(
                select(
                    files_table.c.file_path,
                    files_table.c.module_path,
                    files_table.c.line_count,
                    files_table.c.size_bytes,
                    files_table.c.is_test,
                )
                .where(*filters)
                .order_by(files_table.c.file_path)
                .limit(inp.limit)
                .offset(inp.offset)
            )
            .mappings()
            .all()
        )

        files = [
            {
                "file_path": row["file_path"],
                "module_path": row["module_path"],
                "line_count": int(row["line_count"]),
                "size_bytes": int(row["size_bytes"]),
                "is_test": bool(row["is_test"]),
            }
            for row in rows
        ]

        return success_result(
            {
                "repo_id": resolved_repo_id,
                "total_count": total_count,
                "returned_count": len(files),
                "offset": inp.offset,
                "limit": inp.limit,
                "has_more": (inp.offset + len(files)) < total_count,
                "files": files,
            }
        )


def _sync_get_file_outline(
    file_path: str,
    repo_id: str | None,
    include_private: bool,
    max_depth: int,
    db_path: str | Path | None,
) -> dict[str, Any]:
    """Synchronous implementation for ``get_file_outline``."""
    inp = GetFileOutlineInput(
        file_path=file_path,
        repo_id=repo_id,
        include_private=include_private,
        max_depth=max_depth,
    )
    resolved_db = _resolve_db_path(db_path)

    with connection_scope(resolved_db, read_only=True) as conn:
        initialize_database(conn)
        resolved_repo_id = resolve_repo_id(conn, inp.repo_id)
        outline = build_file_outline(
            conn,
            resolved_repo_id,
            inp.file_path,
            include_private=inp.include_private,
            max_depth=inp.max_depth,
        )
        return success_result(adapt_outline(outline))


def _sync_search_symbols(
    query: str,
    repo_id: str | None,
    kind: str | None,
    file_path: str | None,
    limit: int,
    db_path: str | Path | None,
) -> dict[str, Any]:
    """Synchronous implementation for ``search_symbols``."""
    inp = SearchSymbolsInput(
        query=query,
        repo_id=repo_id,
        kind=kind,  # type: ignore[arg-type]
        file_path=file_path,
        limit=limit,
    )
    resolved_db = _resolve_db_path(db_path)

    with connection_scope(resolved_db, read_only=True) as conn:
        initialize_database(conn)
        resolved_repo_id = resolve_repo_id(conn, inp.repo_id)
        nodes = search_symbols_fts(
            conn,
            resolved_repo_id,
            inp.query,
            kind=inp.kind,
            file_path=inp.file_path,
            limit=inp.limit,
        )
        files_by_id = get_files_by_ids(conn, [n.get("file_id", "") for n in nodes])
        symbols = [
            adapt_node(n, file_info=files_by_id.get(n.get("file_id", "")))
            for n in nodes
        ]
        return success_result(
            {
                "repo_id": resolved_repo_id,
                "query": inp.query,
                "count": len(symbols),
                "symbols": symbols,
            }
        )


def _sync_get_symbol_source(
    symbol: str,
    repo_id: str | None,
    max_lines: int,
    offset: int,
    db_path: str | Path | None,
) -> dict[str, Any]:
    """Synchronous implementation for ``get_symbol_source``."""
    inp = GetSymbolSourceInput(
        symbol=symbol,
        repo_id=repo_id,
        max_lines=max_lines,
        offset=offset,
    )
    resolved_db = _resolve_db_path(db_path)

    with connection_scope(resolved_db, read_only=True) as conn:
        initialize_database(conn)
        resolved_repo_id = resolve_repo_id(conn, inp.repo_id)
        target = _resolve_symbol_target(conn, resolved_repo_id, inp.symbol_id_or_name)
        source_data = read_symbol_source(
            conn,
            target["id"],
            max_lines=inp.max_lines,
            offset=inp.offset,
        )
        cleaned = {k: v for k, v in source_data.items() if v is not None}
        return success_result(cleaned)


def _sync_get_symbol_context(
    symbol: str,
    repo_id: str | None,
    edge_limit: int,
    include_source: bool,
    max_source_lines: int,
    db_path: str | Path | None,
) -> dict[str, Any]:
    """Synchronous implementation for ``get_symbol_context``."""
    inp = GetSymbolContextInput(
        symbol=symbol,
        repo_id=repo_id,
        edge_limit=edge_limit,
        include_source=include_source,
        max_source_lines=max_source_lines,
    )
    resolved_db = _resolve_db_path(db_path)

    with connection_scope(resolved_db, read_only=True) as conn:
        initialize_database(conn)
        resolved_repo_id = resolve_repo_id(conn, inp.repo_id)
        target = _resolve_symbol_target(conn, resolved_repo_id, inp.symbol_id_or_name)
        ctx = build_symbol_context(
            conn,
            target["id"],
            edge_limit=inp.edge_limit,
            include_source=inp.include_source,
            max_source_lines=inp.max_source_lines,
        )
        if ctx is None:
            raise SymbolNotFoundError(
                f"Symbol not found: {inp.symbol_id_or_name}",
                context={"symbol": inp.symbol_id_or_name},
            )
        return success_result({"context": adapt_context(ctx)})


def _load_safe_file_lines(
    repo_root: Path | None,
    file_info: dict[str, Any] | None,
    max_bytes: int,
    cache: dict[str, list[str] | None],
) -> list[str] | None:
    """Safely load and cache lines for a reference site file preview."""
    if repo_root is None or file_info is None:
        return None
    file_id = file_info["id"]
    if file_id in cache:
        return cache[file_id]

    rel_path = file_info.get("file_path")
    if not rel_path:
        cache[file_id] = None
        return None

    try:
        safe_path = ensure_safe_repo_file(repo_root, rel_path)
        text, _ = _read_verified_file_text(safe_path, max_file_size_bytes=max_bytes)
        lines = text.splitlines()
    except (PathSecurityError, ParseError, OSError):
        lines = None

    cache[file_id] = lines
    return lines


def _extract_site_preview(
    edge: dict[str, Any],
    repo_root: Path | None,
    file_info: dict[str, Any] | None,
    max_bytes: int,
    cache: dict[str, list[str] | None],
) -> str | None:
    """Extract a trimmed 1-line source preview at an edge's evidence line."""
    rng = edge.get("evidence_range_json")
    if not isinstance(rng, dict):
        return None
    start = rng.get("start")
    if not isinstance(start, dict):
        return None
    line_idx = start.get("line")
    if not isinstance(line_idx, int) or line_idx < 0:
        return None

    lines = _load_safe_file_lines(repo_root, file_info, max_bytes, cache)
    if not lines or line_idx >= len(lines):
        return None

    trimmed = lines[line_idx].strip()
    if not trimmed:
        return None
    if len(trimmed) > _MAX_PREVIEW_CHARS:
        return trimmed[: _MAX_PREVIEW_CHARS - 3] + "..."
    return trimmed


def _resolve_peer_display_name(
    node_id: str,
    nodes_by_id: dict[str, dict[str, Any]],
    edge: dict[str, Any],
) -> str | None:
    """Resolve human-readable symbol name or unresolved target hint for an edge endpoint."""
    node = nodes_by_id.get(node_id)
    if node is not None:
        return node.get("qualified_name") or node.get("name")
    for prefix in ("external_or_unresolved:", "unresolved_base:", "unresolved_call:"):
        if node_id.startswith(prefix):
            return node_id[len(prefix) :]
    payload = edge.get("payload_json")
    if isinstance(payload, dict):
        return payload.get("target") or payload.get("base_name") or payload.get("module")
    return None


def _sync_get_symbol_references(
    symbol: str,
    repo_id: str | None,
    direction: str,
    refresh_lsp: bool,
    limit: int,
    offset: int,
    db_path: str | Path | None,
    lsp_pool: LspSessionPool | None,
) -> dict[str, Any]:
    """Synchronous implementation for ``get_symbol_references``."""
    inp = GetSymbolReferencesInput(
        symbol=symbol,
        repo_id=repo_id,
        direction=direction,  # type: ignore[arg-type]
        refresh_lsp=refresh_lsp,
        limit=limit,
        offset=offset,
    )
    resolved_db = _resolve_db_path(db_path)
    cfg = get_config()
    pool = lsp_pool if lsp_pool is not None else _DEFAULT_LSP_POOL

    with connection_scope(
        resolved_db,
        read_only=not inp.refresh_lsp,
        must_exist=True,
    ) as conn:
        initialize_database(conn)
        resolved_repo_id = resolve_repo_id(conn, inp.repo_id)
        target = _resolve_symbol_target(conn, resolved_repo_id, inp.symbol_id_or_name)
        repo_record = get_repo_by_id(conn, resolved_repo_id)

        if inp.refresh_lsp:
            file_record = get_file_by_id(conn, target["file_id"])
            if file_record is None or repo_record is None:
                raise DatabaseError(
                    "Repository or file metadata missing for LSP refresh.",
                    error_code="references_unavailable",
                )
            target_symbol = {
                "id": target["id"],
                "repo_id": target["repo_id"],
                "file_id": target["file_id"],
                "file_path": file_record["file_path"],
                "uri": target["uri"],
                "qualified_name": target["qualified_name"],
                "kind": target["kind"],
                "scope": target.get("scope"),
                "range_json": target.get("range_json"),
                "selection_range_json": target.get("selection_range_json"),
                "repo_root": repo_record.root_path,
            }
            client = pool.get_or_start(repo_record.root_path)
            try:
                enrich_references_for_symbol(
                    conn,
                    client,
                    target_symbol,
                    open_all_files=False,
                )
            except Exception:
                if not client.is_running:
                    pool.close_repo(repo_record.root_path)
                raise

        edges = list_symbol_reference_edges(
            conn,
            target["id"],
            direction=inp.direction,
            limit=inp.limit,
            offset=inp.offset,
        )
        total_count = count_symbol_reference_edges(
            conn,
            target["id"],
            direction=inp.direction,
        )
        stats = build_reference_stats(conn, target["id"])

        node_ids_to_fetch: list[str] = []
        file_ids_to_fetch: list[str] = [target["file_id"]]
        for edge in edges:
            node_ids_to_fetch.append(edge["from_id"])
            node_ids_to_fetch.append(edge["to_id"])
            if edge.get("evidence_file_id"):
                file_ids_to_fetch.append(edge["evidence_file_id"])

        nodes_by_id = get_symbols_by_ids(conn, node_ids_to_fetch)
        files_by_id = get_files_by_ids(conn, file_ids_to_fetch)

        repo_root_path: Path | None = None
        if repo_record and repo_record.root_path:
            candidate_root = Path(repo_record.root_path)
            if candidate_root.is_dir():
                repo_root_path = candidate_root

        file_lines_cache: dict[str, list[str] | None] = {}
        items: list[dict[str, Any]] = []
        for edge in edges:
            ev_file = files_by_id.get(edge.get("evidence_file_id") or "")
            preview = _extract_site_preview(
                edge,
                repo_root_path,
                ev_file,
                cfg.max_file_size_bytes,
                file_lines_cache,
            )
            if inp.direction in ("incoming", "outgoing"):
                edge_dir = inp.direction
            elif edge["to_id"] == target["id"] and edge["from_id"] == target["id"]:
                edge_dir = "self"
            elif edge["to_id"] == target["id"]:
                edge_dir = "incoming"
            else:
                edge_dir = "outgoing"

            items.append(
                adapt_edge(
                    edge,
                    preview=preview,
                    from_name=_resolve_peer_display_name(edge["from_id"], nodes_by_id, edge),
                    to_name=_resolve_peer_display_name(edge["to_id"], nodes_by_id, edge),
                    file_path=ev_file.get("file_path") if ev_file else None,
                    direction=edge_dir,
                )
            )

        return success_result(
            {
                "symbol": adapt_node(target, file_info=files_by_id.get(target["file_id"])),
                "direction": inp.direction,
                "total_count": total_count,
                "returned_count": len(items),
                "offset": inp.offset,
                "limit": inp.limit,
                "has_more": (inp.offset + len(items)) < total_count,
                "reference_summary": {k: v for k, v in stats.items() if v is not None},
                "references": items,
            }
        )


def _sync_analyze_risk(
    symbols: list[str],
    repo_id: str | None,
    db_path: str | Path | None,
) -> dict[str, Any]:
    """Synchronous implementation for ``analyze_risk``."""
    inp = AnalyzeRiskInput(symbols=symbols, repo_id=repo_id)
    resolved_db = _resolve_db_path(db_path)

    with connection_scope(resolved_db, read_only=True) as conn:
        initialize_database(conn)
        resolved_repo_id = resolve_repo_id(conn, inp.repo_id)
        resolved_ids: list[str] = []
        for sym in inp.symbols:
            node = _resolve_symbol_target(conn, resolved_repo_id, sym)
            resolved_ids.append(node["id"])

        result = engine_analyze_target_set_risk(conn, resolved_ids)
        return success_result({"risk": adapt_risk_result(result)})


# ==================== Async Tool Entry Points ====================


async def get_repo_overview(
    repo_id: str | None = None,
    db_path: str | Path | None = None,
) -> str:
    """Get repository metadata, symbol/edge counts, and top directories."""
    try:
        payload = await asyncio.to_thread(_sync_get_repo_overview, repo_id, db_path)
        return _serialize_result(payload)
    except Exception as exc:
        return _serialize_result(
            map_exception_to_error_result(exc, operation="get_repo_overview")
        )


async def list_files(
    repo_id: str | None = None,
    path_prefix: str | None = None,
    include_tests: bool = False,
    limit: int = 100,
    offset: int = 0,
    db_path: str | Path | None = None,
) -> str:
    """List indexed files in a repository with pagination and optional prefix filtering."""
    try:
        payload = await asyncio.to_thread(
            _sync_list_files,
            repo_id,
            path_prefix,
            include_tests,
            limit,
            offset,
            db_path,
        )
        return _serialize_result(payload)
    except Exception as exc:
        return _serialize_result(
            map_exception_to_error_result(exc, operation="list_files")
        )


async def get_file_outline(
    file_path: str,
    repo_id: str | None = None,
    include_private: bool = True,
    max_depth: int = 3,
    db_path: str | Path | None = None,
) -> str:
    """Get a hierarchical symbol outline and deduplicated imports for an indexed file."""
    try:
        payload = await asyncio.to_thread(
            _sync_get_file_outline,
            file_path,
            repo_id,
            include_private,
            max_depth,
            db_path,
        )
        return _serialize_result(payload)
    except Exception as exc:
        return _serialize_result(
            map_exception_to_error_result(exc, operation="get_file_outline")
        )


async def search_symbols(
    query: str,
    repo_id: str | None = None,
    kind: str | None = None,
    file_path: str | None = None,
    limit: int = 20,
    db_path: str | Path | None = None,
) -> str:
    """Search repository symbols using FTS5 with exact/prefix ranking."""
    try:
        payload = await asyncio.to_thread(
            _sync_search_symbols,
            query,
            repo_id,
            kind,
            file_path,
            limit,
            db_path,
        )
        return _serialize_result(payload)
    except Exception as exc:
        return _serialize_result(
            map_exception_to_error_result(exc, operation="search_symbols")
        )


async def get_symbol_source(
    symbol: str,
    repo_id: str | None = None,
    max_lines: int = 120,
    offset: int = 0,
    db_path: str | Path | None = None,
) -> str:
    """Read line-numbered source code for a symbol by ID or qualified name."""
    try:
        payload = await asyncio.to_thread(
            _sync_get_symbol_source,
            symbol,
            repo_id,
            max_lines,
            offset,
            db_path,
        )
        return _serialize_result(payload)
    except Exception as exc:
        return _serialize_result(
            map_exception_to_error_result(exc, operation="get_symbol_source")
        )


async def get_symbol_context(
    symbol: str,
    repo_id: str | None = None,
    edge_limit: int = 20,
    include_source: bool = False,
    max_source_lines: int = 80,
    db_path: str | Path | None = None,
) -> str:
    """Get compact symbol hierarchy, callers/callees/imports/inherits, and freshness."""
    try:
        payload = await asyncio.to_thread(
            _sync_get_symbol_context,
            symbol,
            repo_id,
            edge_limit,
            include_source,
            max_source_lines,
            db_path,
        )
        return _serialize_result(payload)
    except Exception as exc:
        return _serialize_result(
            map_exception_to_error_result(exc, operation="get_symbol_context")
        )


async def get_symbol_references(
    symbol: str,
    repo_id: str | None = None,
    direction: DirectionLiteral = "incoming",
    refresh_lsp: bool = False,
    limit: int = 30,
    offset: int = 0,
    db_path: str | Path | None = None,
    lsp_pool: LspSessionPool | None = None,
) -> str:
    """Get LSP references and AST dependency sites with safe 1-line source previews."""
    try:
        payload = await asyncio.to_thread(
            _sync_get_symbol_references,
            symbol,
            repo_id,
            direction,
            refresh_lsp,
            limit,
            offset,
            db_path,
            lsp_pool,
        )
        return _serialize_result(payload)
    except Exception as exc:
        return _serialize_result(
            map_exception_to_error_result(exc, operation="get_symbol_references")
        )


async def analyze_risk(
    symbols: list[str],
    repo_id: str | None = None,
    db_path: str | Path | None = None,
) -> str:
    """Analyze change impact and risk for 1 to 50 target symbols."""
    try:
        payload = await asyncio.to_thread(
            _sync_analyze_risk,
            symbols,
            repo_id,
            db_path,
        )
        return _serialize_result(payload)
    except Exception as exc:
        return _serialize_result(
            map_exception_to_error_result(exc, operation="analyze_risk")
        )


def register_tools(
    mcp: MCPServer[Any] | Any,
    db_path: str | Path | None = None,
    lsp_pool: LspSessionPool | None = None,
) -> None:
    """Register the 8 cleanly named token-lean MCP tools on ``mcp``."""

    @mcp.tool(name="get_repo_overview")
    async def _get_repo_overview(repo_id: str | None = None) -> str:
        """Get repository overview, symbol/edge statistics, and top directories."""
        return await get_repo_overview(repo_id=repo_id, db_path=db_path)

    @mcp.tool(name="list_files")
    async def _list_files(
        repo_id: str | None = None,
        path_prefix: str | None = None,
        include_tests: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> str:
        """List indexed Python files in the repository with pagination and prefix filter."""
        return await list_files(
            repo_id=repo_id,
            path_prefix=path_prefix,
            include_tests=include_tests,
            limit=limit,
            offset=offset,
            db_path=db_path,
        )

    @mcp.tool(name="get_file_outline")
    async def _get_file_outline(
        file_path: str,
        repo_id: str | None = None,
        include_private: bool = True,
        max_depth: int = 3,
    ) -> str:
        """Get a hierarchical symbol skeleton and imports for a repository file."""
        return await get_file_outline(
            file_path=file_path,
            repo_id=repo_id,
            include_private=include_private,
            max_depth=max_depth,
            db_path=db_path,
        )

    @mcp.tool(name="search_symbols")
    async def _search_symbols(
        query: str,
        repo_id: str | None = None,
        kind: SymbolKindLiteral | None = None,
        file_path: str | None = None,
        limit: int = 20,
    ) -> str:
        """Search symbols by name, signature, or docstring using FTS5 ranking."""
        return await search_symbols(
            query=query,
            repo_id=repo_id,
            kind=kind,
            file_path=file_path,
            limit=limit,
            db_path=db_path,
        )

    @mcp.tool(name="get_symbol_source")
    async def _get_symbol_source(
        symbol: str,
        repo_id: str | None = None,
        max_lines: int = 120,
        offset: int = 0,
    ) -> str:
        """Read bounded, line-numbered source code for a symbol ID or qualified name."""
        return await get_symbol_source(
            symbol=symbol,
            repo_id=repo_id,
            max_lines=max_lines,
            offset=offset,
            db_path=db_path,
        )

    @mcp.tool(name="get_symbol_context")
    async def _get_symbol_context(
        symbol: str,
        repo_id: str | None = None,
        edge_limit: int = 20,
        include_source: bool = False,
        max_source_lines: int = 80,
    ) -> str:
        """Get bounded symbol context: hierarchy, callers/callees/imports/inherits, and freshness."""
        return await get_symbol_context(
            symbol=symbol,
            repo_id=repo_id,
            edge_limit=edge_limit,
            include_source=include_source,
            max_source_lines=max_source_lines,
            db_path=db_path,
        )

    @mcp.tool(name="get_symbol_references")
    async def _get_symbol_references(
        symbol: str,
        repo_id: str | None = None,
        direction: DirectionLiteral = "incoming",
        refresh_lsp: bool = False,
        limit: int = 30,
        offset: int = 0,
    ) -> str:
        """Get paginated LSP references and AST dependencies with 1-line source previews."""
        return await get_symbol_references(
            symbol=symbol,
            repo_id=repo_id,
            direction=direction,
            refresh_lsp=refresh_lsp,
            limit=limit,
            offset=offset,
            db_path=db_path,
            lsp_pool=lsp_pool,
        )

    @mcp.tool(name="analyze_risk")
    async def _analyze_risk(
        symbols: list[str],
        repo_id: str | None = None,
    ) -> str:
        """Analyze change risk, blast radius, and staleness for 1 to 50 target symbols."""
        return await analyze_risk(
            symbols=symbols,
            repo_id=repo_id,
            db_path=db_path,
        )
