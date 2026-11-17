"""Handlers for `rc symbols {search,list,show}` and `rc source`."""

from __future__ import annotations

import argparse
from typing import Any

from repo_context.context import build_symbol_context, read_symbol_source
from repo_context.exceptions import SymbolNotFoundError, ValidationError
from repo_context.graph import get_files_by_ids
from repo_context.storage import (
    connection_scope,
    initialize_database,
    resolve_repo_id,
    search_nodes_fts,
)
from repo_context.storage.nodes import row_to_node
from repo_context.cli.formatting import (
    format_ascii_table,
    format_key_value_section,
    format_line_range,
    format_location,
    print_json,
)
from repo_context.cli.utils import (
    _escape_like,
    resolve_db_path,
    resolve_symbol_target,
)


def _format_symbol_row(node: dict[str, Any], file_info: dict[str, Any] | None) -> list[Any]:
    """Build standard table row ``[ID, Kind, Location, Qualified Name, Signature]``."""
    file_path = file_info.get("file_path") if file_info else None
    location = format_location(
        file_path,
        start_line=node.get("start_line"),
        end_line=node.get("end_line"),
    )
    return [
        node["id"],
        node["kind"],
        location,
        node["qualified_name"],
        node.get("signature") or node["name"],
    ]


def _serialize_symbol_entry(node: dict[str, Any], file_info: dict[str, Any] | None) -> dict[str, Any]:
    """Build clean JSON dict for search/list results."""
    return {
        "id": node["id"],
        "kind": node["kind"],
        "name": node["name"],
        "qualified_name": node["qualified_name"],
        "file_path": file_info.get("file_path") if file_info else None,
        "module_path": file_info.get("module_path") if file_info else None,
        "start_line": node.get("start_line"),
        "end_line": node.get("end_line"),
        "signature": node.get("signature"),
        "doc_summary": node.get("doc_summary"),
    }


def _normalize_file_filter(file_filter: str) -> str:
    """Strip leading ``./`` segments and trailing slashes without mangling dot-directories."""
    cleaned = file_filter.strip()
    while cleaned.startswith("./"):
        cleaned = cleaned.removeprefix("./")
    return cleaned.rstrip("/")


def cmd_symbols_search(args: argparse.Namespace) -> int:
    """Search symbols via FTS5 with ranking boost and LIKE fallback."""
    db_path = resolve_db_path(args)
    limit = int(getattr(args, "limit", 20))
    if limit < 1:
        raise ValidationError("--limit must be >= 1", context={"limit": limit})
    repo_arg: str | None = getattr(args, "repo", None)
    kind: str | None = getattr(args, "kind", None)
    raw_file_filter: str | None = getattr(args, "file", None)
    file_filter = _normalize_file_filter(raw_file_filter) if raw_file_filter is not None else None

    with connection_scope(db_path, read_only=True) as conn:
        initialize_database(conn)
        repo_id = resolve_repo_id(conn, repo_arg)
        nodes = search_nodes_fts(
            conn,
            repo_id,
            args.query,
            kind=kind,
            file_path=file_filter,
            limit=limit,
        )
        files_by_id = get_files_by_ids(conn, [n.get("file_id", "") for n in nodes])

    if getattr(args, "json", False):
        print_json(
            {
                "repo_id": repo_id,
                "query": args.query,
                "count": len(nodes),
                "symbols": [
                    _serialize_symbol_entry(n, files_by_id.get(n.get("file_id", "")))
                    for n in nodes
                ],
            }
        )
        return 0

    print(
        format_ascii_table(
            ["ID", "Kind", "Location", "Qualified Name", "Signature"],
            [
                _format_symbol_row(n, files_by_id.get(n.get("file_id", "")))
                for n in nodes
            ],
            max_col_widths=[None, 20, None, 48, 64],
            empty_message=f"(no symbols matching {args.query!r})",
        )
    )
    return 0


def cmd_symbols_list(args: argparse.Namespace) -> int:
    """List symbols with pagination and optional kind/file filters."""
    db_path = resolve_db_path(args)
    limit = int(getattr(args, "limit", 50))
    offset = int(getattr(args, "offset", 0))
    if limit < 1:
        raise ValidationError("--limit must be >= 1", context={"limit": limit})
    if offset < 0:
        raise ValidationError("--offset must be >= 0", context={"offset": offset})

    repo_arg: str | None = getattr(args, "repo", None)
    kind: str | None = getattr(args, "kind", None)
    file_filter: str | None = getattr(args, "file", None)

    with connection_scope(db_path, read_only=True) as conn:
        initialize_database(conn)
        repo_id = resolve_repo_id(conn, repo_arg)

        where_clauses = ["n.repo_id = ?"]
        params: list[Any] = [repo_id]
        if kind is not None:
            where_clauses.append("n.kind = ?")
            params.append(kind)
        if file_filter is not None:
            cleaned = _normalize_file_filter(file_filter)
            where_clauses.append("(f.file_path = ? OR f.file_path LIKE ? ESCAPE '\\')")
            params.extend([cleaned, f"{_escape_like(cleaned)}/%"])

        where_sql = " AND ".join(where_clauses)
        total_count = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM nodes n
                JOIN files f ON f.id = n.file_id
                WHERE {where_sql}
                """,
                tuple(params),
            ).fetchone()[0]
        )

        rows = conn.execute(
            f"""
            SELECT n.id, n.repo_id, n.file_id, n.language, n.kind, n.name, n.qualified_name, n.uri,
                   n.range_json, n.selection_range_json, n.start_line, n.end_line, n.signature,
                   n.parent_id, n.visibility_hint, n.doc_summary, n.content_hash, n.semantic_hash,
                   n.source, n.confidence, n.payload_json, n.scope, n.lexical_parent_id, n.last_indexed_at,
                   f.file_path AS _file_path, f.module_path AS _module_path
            FROM nodes n
            JOIN files f ON f.id = n.file_id
            WHERE {where_sql}
            ORDER BY f.file_path, COALESCE(n.start_line, 0), n.kind, n.qualified_name, n.id
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()

        nodes = [row_to_node(r) for r in rows]
        file_meta = {
            r["id"]: {"file_path": r["_file_path"], "module_path": r["_module_path"]}
            for r in rows
        }

    if getattr(args, "json", False):
        print_json(
            {
                "repo_id": repo_id,
                "total_count": total_count,
                "returned_count": len(nodes),
                "offset": offset,
                "limit": limit,
                "has_more": (offset + len(nodes)) < total_count,
                "symbols": [
                    _serialize_symbol_entry(n, file_meta.get(n["id"]))
                    for n in nodes
                ],
            }
        )
        return 0

    print(
        format_ascii_table(
            ["ID", "Kind", "Qualified Name", "Location", "Signature"],
            [
                [
                    n["id"],
                    n["kind"],
                    n["qualified_name"],
                    format_location(
                        (file_meta.get(n["id"]) or {}).get("file_path"),
                        start_line=n.get("start_line"),
                        end_line=n.get("end_line"),
                    ),
                    n.get("signature") or n["name"],
                ]
                for n in nodes
            ],
            max_col_widths=[None, 20, 48, None, 64],
            empty_message="(no matching symbols)",
        )
    )
    if total_count > len(nodes) or offset > 0:
        start_idx = offset + 1 if nodes else 0
        end_idx = offset + len(nodes)
        print(f"Showing {start_idx}-{end_idx} of {total_count} symbols.")
    return 0


def cmd_symbols_show(args: argparse.Namespace) -> int:
    """Show rich symbol context via ``build_symbol_context``."""
    db_path = resolve_db_path(args)
    repo_arg: str | None = getattr(args, "repo", None)
    include_source = bool(getattr(args, "source", False))
    id_or_name: str = args.id_or_name

    with connection_scope(db_path, read_only=True) as conn:
        initialize_database(conn)
        repo_id = (
            resolve_repo_id(conn, repo_arg)
            if (repo_arg is not None or not id_or_name.strip().startswith("sym:"))
            else None
        )
        target = resolve_symbol_target(conn, repo_id, id_or_name)
        ctx = build_symbol_context(
            conn,
            target["id"],
            include_source=include_source,
            max_source_lines=120,
        )
        if ctx is None:
            raise SymbolNotFoundError(
                f"Symbol not found: {target['id']}",
                context={"symbol": target["id"]},
            )

    if getattr(args, "json", False):
        print_json(ctx.model_dump())
        return 0

    focus = ctx.focus_symbol
    struct_parent = ctx.structural_parent
    lex_parent = ctx.lexical_parent
    struct_sum = ctx.structural_summary or {}
    ref_sum = ctx.reference_summary or {}
    freshness = ctx.freshness or {}
    confidence = ctx.confidence or {}

    sections: list[str] = [
        format_key_value_section(
            "Focus Symbol",
            [
                ("ID", focus["id"]),
                ("Kind", focus.get("kind")),
                ("Qualified Name", focus.get("qualified_name")),
                (
                    "Location",
                    format_location(
                        focus.get("file_path"),
                        start_line=focus.get("start_line"),
                        end_line=focus.get("end_line"),
                    ),
                ),
                ("Signature", focus.get("signature") or focus.get("name")),
                ("Scope", focus.get("scope") or "-"),
                ("Visibility", focus.get("visibility_hint") or "-"),
                ("Doc Summary", focus.get("doc_summary") or "-"),
                (
                    "Structural Parent",
                    f"{struct_parent['qualified_name']} ({struct_parent['kind']}) [{struct_parent['id']}]"
                    if struct_parent
                    else "None",
                ),
                (
                    "Lexical Parent",
                    f"{lex_parent['qualified_name']} ({lex_parent['kind']}) [{lex_parent['id']}]"
                    if lex_parent
                    else "None",
                ),
            ],
        )
    ]

    if ctx.structural_children:
        sections.append("")
        sections.append(
            f"Structural Children ({struct_sum.get('structural_child_count', len(ctx.structural_children))})"
        )
        sections.append(
            format_ascii_table(
                ["ID", "Kind", "Name", "Lines", "Signature"],
                [
                    [
                        c["id"],
                        c["kind"],
                        c["name"],
                        format_line_range(lines=c.get("lines")),
                        c.get("signature") or c["name"],
                    ]
                    for c in ctx.structural_children
                ],
                max_col_widths=[None, 18, 32, 14, 64],
            )
        )

    if ctx.lexical_children:
        sections.append("")
        sections.append(
            f"Lexical Children ({struct_sum.get('lexical_child_count', len(ctx.lexical_children))})"
        )
        sections.append(
            format_ascii_table(
                ["ID", "Kind", "Name", "Lines", "Signature"],
                [
                    [
                        c["id"],
                        c["kind"],
                        c["name"],
                        format_line_range(lines=c.get("lines")),
                        c.get("signature") or c["name"],
                    ]
                    for c in ctx.lexical_children
                ],
                max_col_widths=[None, 20, 32, 14, 64],
            )
        )

    if ctx.incoming_edges:
        sections.append("")
        sections.append(
            f"Incoming Relationships ({struct_sum.get('incoming_edge_count', len(ctx.incoming_edges))})"
        )
        sections.append(
            format_ascii_table(
                ["Kind", "From ID", "Peer", "Location", "Conf"],
                [
                    [
                        e.get("kind"),
                        e.get("from_id"),
                        e.get("peer_name") or "-",
                        format_location(e.get("file_path"), start_line=e.get("line")),
                        e.get("confidence"),
                    ]
                    for e in ctx.incoming_edges
                ],
                max_col_widths=[14, None, 40, None, 6],
            )
        )

    if ctx.outgoing_edges:
        sections.append("")
        sections.append(
            f"Outgoing Relationships ({struct_sum.get('outgoing_edge_count', len(ctx.outgoing_edges))})"
        )
        sections.append(
            format_ascii_table(
                ["Kind", "To ID", "Peer", "Location", "Conf"],
                [
                    [
                        e.get("kind"),
                        e.get("to_id"),
                        e.get("peer_name") or "-",
                        format_location(e.get("file_path"), start_line=e.get("line")),
                        e.get("confidence"),
                    ]
                    for e in ctx.outgoing_edges
                ],
                max_col_widths=[14, None, 40, None, 6],
            )
        )

    if ctx.file_siblings:
        sections.append("")
        sections.append(
            f"File Siblings ({struct_sum.get('same_file_sibling_count', len(ctx.file_siblings))})"
        )
        sections.append(
            format_ascii_table(
                ["ID", "Kind", "Name", "Lines", "Signature"],
                [
                    [
                        s["id"],
                        s["kind"],
                        s["name"],
                        format_line_range(lines=s.get("lines")),
                        s.get("signature") or s["name"],
                    ]
                    for s in ctx.file_siblings
                ],
                max_col_widths=[None, 18, 32, 14, 64],
            )
        )

    edge_conf_min = confidence.get("edge_confidence_min")
    edge_conf_max = confidence.get("edge_confidence_max")
    edge_conf_range = (
        f"{edge_conf_min}-{edge_conf_max}"
        if edge_conf_min is not None and edge_conf_max is not None
        else "-"
    )

    sections.append("")
    sections.append(
        format_key_value_section(
            "Summaries",
            [
                ("Reference Source", ref_sum.get("reference_source", "-")),
                ("Reference Count", ref_sum.get("reference_count", 0)),
                ("Referencing Files", ref_sum.get("referencing_file_count", 0)),
                ("Referencing Modules", ref_sum.get("referencing_module_count", 0)),
                ("LSP Refreshed", ref_sum.get("available", False)),
                ("Context Source", freshness.get("context_source", "-")),
                ("Disk Fresh", freshness.get("is_fresh", "-")),
                ("Stale Reason", freshness.get("stale_reason") or "-"),
                ("Symbol Confidence", confidence.get("focus_symbol_confidence", "-")),
                ("Edge Confidence", edge_conf_range),
                ("Placeholders", confidence.get("contains_placeholder_targets", "-")),
            ],
        )
    )

    if include_source:
        sections.append("")
        if focus.get("source"):
            sections.append("Source")
            sections.append("------")
            sections.append(focus["source"])
            if focus.get("source_truncated"):
                sections.append("... (source truncated; use `rc source` to paginate)")
        elif focus.get("source_error"):
            sections.append(f"Source unavailable: {focus['source_error']}")

    print("\n".join(sections))
    return 0


def cmd_source(args: argparse.Namespace) -> int:
    """Display line-numbered source snippet for a symbol via ``read_symbol_source``."""
    db_path = resolve_db_path(args)
    repo_arg: str | None = getattr(args, "repo", None)
    max_lines = int(getattr(args, "max_lines", 120))
    offset = int(getattr(args, "offset", 0))
    id_or_name: str = args.id_or_name

    with connection_scope(db_path, read_only=True) as conn:
        initialize_database(conn)
        repo_id = (
            resolve_repo_id(conn, repo_arg)
            if (repo_arg is not None or not id_or_name.strip().startswith("sym:"))
            else None
        )
        target = resolve_symbol_target(conn, repo_id, id_or_name)
        data = read_symbol_source(
            conn,
            target["id"],
            max_lines=max_lines,
            offset=offset,
        )

    if getattr(args, "json", False):
        print_json(data)
        return 0

    freshness = "fresh" if data.get("is_fresh", True) else f"stale ({data.get('stale_reason')})"
    header = format_key_value_section(
        "Symbol Source",
        [
            ("ID", data["symbol_id"]),
            ("Kind", data["kind"]),
            ("Qualified Name", data["qualified_name"]),
            ("Location", format_location(data["file_path"], lines=data.get("lines"))),
            ("Signature", data.get("signature") or "-"),
            ("Freshness", freshness),
            ("Lines Returned", f"{data['returned_lines']}/{data['total_lines']}"),
        ],
    )
    parts = [header, "", data.get("source") or "(empty)"]
    if data.get("truncated") and data.get("next_offset") is not None:
        parts.append(
            f"\n--- truncated ({data['returned_lines']}/{data['total_lines']} lines shown; "
            f"continue with --offset {data['next_offset']}) ---"
        )
    print("\n".join(parts))
    return 0
