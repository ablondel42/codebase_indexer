"""Hierarchical file outline builder."""

from collections import defaultdict
import sqlite3

from repo_context.constants import SYMBOL_KIND_MODULE
from repo_context.context.helpers import format_line_span
from repo_context.exceptions import RepoContextError, ValidationError
from repo_context.graph.queries import (
    check_file_disk_freshness,
    get_file_for_outline,
    list_file_import_edges,
    list_file_outline_symbols,
)


def _is_private_symbol(node: dict) -> bool:
    """Return True if a symbol is private-like or function-local."""
    visibility = node.get("visibility_hint")
    if visibility in ("private_like", "local"):
        return True
    name = node.get("name") or ""
    if name.startswith("_") and not (name.startswith("__") and name.endswith("__")):
        return True
    return False


def _format_import_label(edge: dict) -> str | None:
    """Format an import edge into a concise deduplicated import string."""
    payload = edge.get("payload_json") or {}
    if not isinstance(payload, dict):
        return None
    import_type = payload.get("import_type")
    module_name = payload.get("module") or ""
    symbol_name = payload.get("symbol") or ""
    alias = payload.get("alias")

    if import_type == "import" or not symbol_name:
        base = module_name
    else:
        base = f"{module_name}.{symbol_name}" if module_name else symbol_name

    if not base:
        return None
    if alias:
        return f"{base} as {alias}"
    return base


def _collect_deduplicated_imports(
    import_edges: list[dict],
    module_node: dict | None,
) -> list[str]:
    """Collect deduplicated import descriptors ordered by appearance."""
    seen: set[str] = set()
    imports: list[str] = []

    for edge in import_edges:
        label = _format_import_label(edge)
        if label and label not in seen:
            seen.add(label)
            imports.append(label)

    if not imports and module_node is not None:
        payload = module_node.get("payload_json") or {}
        if isinstance(payload, dict):
            for mod in payload.get("imported_modules") or []:
                if isinstance(mod, str) and mod and mod not in seen:
                    seen.add(mod)
                    imports.append(mod)

    return imports


def build_file_outline(
    conn: sqlite3.Connection,
    repo_id: str,
    file_path: str,
    *,
    include_private: bool = True,
    max_depth: int = 3,
) -> dict:
    """Build a compact hierarchical symbol outline for one indexed file.

    Args:
        conn: SQLite connection.
        repo_id: Repository ID.
        file_path: Repository-relative POSIX file path.
        include_private: Whether to include private/local symbols.
        max_depth: Maximum symbol nesting depth to expand (1 = top-level only).

    Returns:
        Dictionary containing ``file_path``, ``module_path``, ``line_count``,
        ``is_fresh``, ``doc``, deduplicated ``imports``, and nested ``symbols``.
    """
    if max_depth < 0:
        raise ValidationError("max_depth must be >= 0", context={"max_depth": max_depth})

    file_record = get_file_for_outline(conn, repo_id, file_path)
    if file_record is None:
        raise RepoContextError(
            f"File not found in index: {file_path}",
            error_code="file_not_found",
            context={"repo_id": repo_id, "file_path": file_path},
        )

    is_fresh, stale_reason = check_file_disk_freshness(
        conn, file_record["id"], file_record=file_record
    )

    nodes = list_file_outline_symbols(conn, file_record["id"])
    import_edges = list_file_import_edges(conn, file_record["id"])

    module_node: dict | None = None
    non_module_nodes: list[dict] = []
    for node in nodes:
        if node.get("kind") == SYMBOL_KIND_MODULE and module_node is None:
            module_node = node
        else:
            non_module_nodes.append(node)

    module_node_id = module_node["id"] if module_node is not None else None
    non_module_ids = {n["id"] for n in non_module_nodes}

    children_by_parent: dict[str | None, list[dict]] = defaultdict(list)
    for node in non_module_nodes:
        parent_id = node.get("parent_id")
        if parent_id == module_node_id or parent_id is None or parent_id not in non_module_ids:
            children_by_parent[None].append(node)
        else:
            children_by_parent[parent_id].append(node)

    def _build_subtree(parent_key: str | None, depth: int) -> list[dict]:
        if depth > max_depth:
            return []
        items: list[dict] = []
        for child in children_by_parent.get(parent_key, []):
            if not include_private and _is_private_symbol(child):
                continue
            nested = _build_subtree(child["id"], depth + 1) if depth < max_depth else []
            items.append(
                {
                    "id": child["id"],
                    "kind": child["kind"],
                    "name": child["name"],
                    "lines": format_line_span(child),
                    "signature": child.get("signature"),
                    "doc": child.get("doc_summary"),
                    "children": nested,
                }
            )
        return items

    symbols = _build_subtree(None, 1) if max_depth >= 1 else []
    imports = _collect_deduplicated_imports(import_edges, module_node)

    outline = {
        "file_path": file_record["file_path"],
        "module_path": file_record["module_path"],
        "line_count": int(file_record.get("line_count", 0)),
        "is_fresh": is_fresh,
        "doc": module_node.get("doc_summary") if module_node is not None else None,
        "imports": imports,
        "symbols": symbols,
    }
    if not is_fresh and stale_reason:
        outline["stale_reason"] = stale_reason
    return outline
