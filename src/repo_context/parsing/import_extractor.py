"""Import extraction from Python AST with relative import resolution."""

import ast
import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from repo_context.constants import EDGE_KIND_IMPORTS
from repo_context.models.file import FileRecord
from repo_context.parsing.ranges import make_range


@dataclass
class _ImportAccumulator:
    """Mutable accumulator for import edges and deduplicated module/symbol lists."""

    edges: list[dict] = field(default_factory=list)
    imported_modules: list[str] = field(default_factory=list)
    seen_modules: set[str] = field(default_factory=set)
    imported_symbols: list[str] = field(default_factory=list)
    seen_symbols: set[str] = field(default_factory=set)

    def record_module(self, module_name: str) -> None:
        """Record an imported module name preserving discovery order."""
        if module_name and module_name not in self.seen_modules:
            self.seen_modules.add(module_name)
            self.imported_modules.append(module_name)

    def record_symbol(self, symbol_name: str) -> None:
        """Record an imported symbol name preserving discovery order."""
        if symbol_name not in self.seen_symbols:
            self.seen_symbols.add(symbol_name)
            self.imported_symbols.append(symbol_name)


def _resolve_relative_module(file_record: FileRecord, raw_module: str, level: int) -> str:
    """Resolve a relative ``from ...`` import against ``file_record.module_path``.

    Args:
        file_record: Source file record.
        raw_module: Raw ``node.module`` string (empty string for ``from . import x``).
        level: Relative dot count (0 for absolute imports).

    Returns:
        Resolved dotted module path, or ``raw_module`` fallback when level exceeds package depth.
    """
    if level <= 0:
        return raw_module

    module_path = file_record.module_path or ""
    parts = [p for p in module_path.split(".") if p]
    is_package_init = (
        file_record.file_path == "__init__.py"
        or file_record.file_path.endswith("/__init__.py")
    )
    package_parts = parts if is_package_init else parts[:-1]

    up_steps = level - 1
    if up_steps > len(package_parts):
        return raw_module

    base_parts = package_parts[: len(package_parts) - up_steps]
    resolved_parts = (
        [*base_parts, *[p for p in raw_module.split(".") if p]]
        if raw_module
        else base_parts
    )
    return ".".join(resolved_parts)


def _process_import_node(
    repo_id: str,
    module_node_id: str,
    file_record: FileRecord,
    node: ast.Import,
    evidence_range_json: str | None,
    acc: _ImportAccumulator,
) -> None:
    """Extract edges and module records from an ``ast.Import`` node."""
    lineno = getattr(node, "lineno", 0)
    col_offset = getattr(node, "col_offset", 0)
    for alias_idx, alias in enumerate(node.names):
        module_name = alias.name
        acc.record_module(module_name)
        to_id = f"external_or_unresolved:{module_name}"
        acc.edges.append(
            {
                "id": _make_import_edge_id(
                    repo_id, module_node_id, to_id, lineno, col_offset, alias_idx
                ),
                "repo_id": repo_id,
                "kind": EDGE_KIND_IMPORTS,
                "from_id": module_node_id,
                "to_id": to_id,
                "source": "python-ast",
                "confidence": 0.8,
                "evidence_file_id": file_record.id,
                "evidence_uri": file_record.uri,
                "evidence_range_json": evidence_range_json,
                "payload_json": json.dumps(
                    {
                        "alias": alias.asname,
                        "module": module_name,
                        "level": 0,
                        "import_type": "import",
                    },
                    sort_keys=True,
                ),
                "last_indexed_at": file_record.last_indexed_at,
            }
        )


def _build_from_import_payload(
    alias: ast.alias,
    raw_module: str,
    resolved_module: str,
    level: int,
) -> dict:
    """Build payload dictionary for a ``from ... import ...`` alias."""
    payload: dict = {
        "alias": alias.asname,
        "module": resolved_module,
        "symbol": alias.name,
        "level": level,
        "import_type": "from",
    }
    if level > 0 and raw_module != resolved_module:
        payload["raw_module"] = raw_module
    return payload


def _process_import_from_node(
    repo_id: str,
    module_node_id: str,
    file_record: FileRecord,
    node: ast.ImportFrom,
    evidence_range_json: str | None,
    acc: _ImportAccumulator,
) -> None:
    """Extract edges, module records, and symbol records from an ``ast.ImportFrom`` node."""
    raw_module = node.module or ""
    resolved_module = _resolve_relative_module(file_record, raw_module, node.level)
    acc.record_module(resolved_module)
    lineno = getattr(node, "lineno", 0)
    col_offset = getattr(node, "col_offset", 0)

    for alias_idx, alias in enumerate(node.names):
        acc.record_symbol(alias.name)
        target = f"{resolved_module}.{alias.name}" if resolved_module else alias.name
        to_id = f"external_or_unresolved:{target}"
        acc.edges.append(
            {
                "id": _make_import_edge_id(
                    repo_id, module_node_id, to_id, lineno, col_offset, alias_idx
                ),
                "repo_id": repo_id,
                "kind": EDGE_KIND_IMPORTS,
                "from_id": module_node_id,
                "to_id": to_id,
                "source": "python-ast",
                "confidence": 0.8,
                "evidence_file_id": file_record.id,
                "evidence_uri": file_record.uri,
                "evidence_range_json": evidence_range_json,
                "payload_json": json.dumps(
                    _build_from_import_payload(alias, raw_module, resolved_module, node.level),
                    sort_keys=True,
                ),
                "last_indexed_at": file_record.last_indexed_at,
            }
        )


def extract_import_edges_and_payload(
    repo_id: str,
    module_node_id: str,
    file_record: FileRecord,
    tree: ast.Module,
    *,
    lines: Sequence[str] | None = None,
) -> tuple[list[dict], list[str], list[str]]:
    """Extract import edges and deduplicated module payload data from AST.

    Args:
        repo_id: Repository ID.
        module_node_id: Module node ID.
        file_record: File record for the source module.
        tree: Parsed AST module.
        lines: Optional pre-split source lines for exact range offsets.

    Returns:
        Tuple of ``(import edges list, deduplicated imported_modules,
        deduplicated imported_symbols)``.
    """
    acc = _ImportAccumulator()
    import_nodes = [
        node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    import_nodes.sort(key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0)))

    for node in import_nodes:
        evidence_range = make_range(node, lines=lines)
        evidence_range_json = (
            json.dumps(evidence_range, sort_keys=True) if evidence_range else None
        )
        if isinstance(node, ast.Import):
            _process_import_node(
                repo_id, module_node_id, file_record, node, evidence_range_json, acc
            )
        else:
            _process_import_from_node(
                repo_id, module_node_id, file_record, node, evidence_range_json, acc
            )

    return acc.edges, acc.imported_modules, acc.imported_symbols


def _make_import_edge_id(
    repo_id: str,
    from_id: str,
    to_id: str,
    lineno: int,
    col_offset: int,
    alias_idx: int,
) -> str:
    """Create a deterministic, collision-free edge ID for an import edge."""
    return (
        f"edge:{repo_id}:{EDGE_KIND_IMPORTS}:"
        f"{from_id}->{to_id}:{lineno}:{col_offset}:{alias_idx}"
    )
