"""Python AST ``imports`` edge extractor service."""

import ast
from collections.abc import Sequence
from typing import Any

from core.models.edge import Edge
from core.models.enums import PREFIX_EDGE, EdgeKind
from core.models.file import FileRecord
from core.models.range import Range
from core.utils.logging import get_logger
from parsing.models.extraction import ImportEntry, ImportExtractionResult
from parsing.utils.ast_helpers import resolve_relative_module
from parsing.utils.ranges import make_range

logger = get_logger("parsing.services.imports")


class ImportExtractor:
    """Extracts ``imports`` ``Edge`` items and deduplicated module/symbol metadata."""

    @staticmethod
    def _make_import_edge_id(
        file_record: FileRecord,
        from_id: str,
        to_id: str,
        node: ast.AST,
        alias_idx: int,
    ) -> str:
        """Create a deterministic, collision-free edge ID for an ``imports`` edge.

        Args:
            file_record: Source ``FileRecord``.
            from_id: Importing module ``SymbolNode`` ID.
            to_id: Initial unresolved placeholder target ID.
            node: ``ast.Import`` or ``ast.ImportFrom`` node.
            alias_idx: Zero-based index of the alias within ``node.names``.

        Returns:
            Deterministic ``edge:<repo_id>:imports:...`` identifier.
        """
        lineno = getattr(node, "lineno", 0)
        site_col = getattr(node, "col_offset", 0) + 1
        return (
            f"{PREFIX_EDGE}{file_record.repo_id}:{EdgeKind.IMPORTS.value}:"
            f"{from_id}@{lineno}:{site_col}:{alias_idx}->{to_id}"
        )

    def _entries_for_import(
        self,
        file_record: FileRecord,
        module_node_id: str,
        node: ast.Import,
        evidence_range: Range | None,
    ) -> list[ImportEntry]:
        """Extract ``ImportEntry`` items from an ``ast.Import`` node."""
        entries: list[ImportEntry] = []
        for alias_idx, alias in enumerate(node.names):
            module_name = alias.name
            placeholder_id = f"external_or_unresolved:{module_name}"
            edge = Edge(
                id=self._make_import_edge_id(
                    file_record,
                    module_node_id,
                    placeholder_id,
                    node,
                    alias_idx,
                ),
                repo_id=file_record.repo_id,
                kind=EdgeKind.IMPORTS,
                from_id=module_node_id,
                to_id=placeholder_id,
                source="python-ast",
                confidence=0.8,
                evidence_file_id=file_record.id,
                evidence_uri=file_record.uri,
                evidence_range=evidence_range,
                payload={
                    "alias": alias.asname,
                    "module": module_name,
                    "level": 0,
                    "import_type": "import",
                    "unresolved_target": placeholder_id,
                },
                last_indexed_at=file_record.last_indexed_at,
            )
            entries.append(ImportEntry(edge=edge, module_name=module_name))
        return entries

    @staticmethod
    def _build_from_payload(
        alias: ast.alias,
        raw_module: str,
        resolved_module: str,
        level: int,
        placeholder_id: str,
    ) -> dict[str, Any]:
        """Build payload metadata for a ``from ... import ...`` alias."""
        payload: dict[str, Any] = {
            "alias": alias.asname,
            "module": resolved_module,
            "symbol": alias.name,
            "imported_name": alias.name,
            "level": level,
            "import_type": "from",
            "unresolved_target": placeholder_id,
        }
        if level > 0 and raw_module != resolved_module:
            payload["raw_module"] = raw_module
        return payload

    def _entries_for_import_from(
        self,
        file_record: FileRecord,
        module_node_id: str,
        node: ast.ImportFrom,
        evidence_range: Range | None,
    ) -> list[ImportEntry]:
        """Extract ``ImportEntry`` items from an ``ast.ImportFrom`` node."""
        raw_module = node.module or ""
        resolved_module = resolve_relative_module(file_record, raw_module, node.level)
        entries: list[ImportEntry] = []

        for alias_idx, alias in enumerate(node.names):
            target = f"{resolved_module}.{alias.name}" if resolved_module else alias.name
            placeholder_id = f"external_or_unresolved:{target}"
            edge = Edge(
                id=self._make_import_edge_id(
                    file_record,
                    module_node_id,
                    placeholder_id,
                    node,
                    alias_idx,
                ),
                repo_id=file_record.repo_id,
                kind=EdgeKind.IMPORTS,
                from_id=module_node_id,
                to_id=placeholder_id,
                source="python-ast",
                confidence=0.8,
                evidence_file_id=file_record.id,
                evidence_uri=file_record.uri,
                evidence_range=evidence_range,
                payload=self._build_from_payload(
                    alias,
                    raw_module,
                    resolved_module,
                    node.level,
                    placeholder_id,
                ),
                last_indexed_at=file_record.last_indexed_at,
            )
            entries.append(
                ImportEntry(
                    edge=edge,
                    module_name=resolved_module,
                    symbol_name=alias.name,
                )
            )
        return entries

    @staticmethod
    def _extract_bound_info(entries: Sequence[ImportEntry]) -> tuple[frozenset[str], bool]:
        """Collect file-level bound identifier names and detect wildcard imports."""
        bound: set[str] = set()
        has_star = False
        for entry in entries:
            payload = entry.edge.payload
            alias = payload.get("alias")
            if payload.get("import_type") == "import":
                if isinstance(alias, str) and alias:
                    bound.add(alias)
                elif entry.module_name:
                    bound.add(entry.module_name.split(".")[0])
            elif entry.symbol_name == "*":
                has_star = True
            elif isinstance(alias, str) and alias:
                bound.add(alias)
            elif entry.symbol_name:
                bound.add(entry.symbol_name)
        return frozenset(bound), has_star

    @classmethod
    def _aggregate_entries(cls, entries: Sequence[ImportEntry]) -> ImportExtractionResult:
        """Aggregate ``ImportEntry`` items into deduplicated ``ImportExtractionResult``."""
        edges: list[Edge] = []
        imported_modules: list[str] = []
        seen_modules: set[str] = set()
        imported_symbols: list[str] = []
        seen_symbols: set[str] = set()

        for entry in entries:
            edges.append(entry.edge)
            if entry.module_name and entry.module_name not in seen_modules:
                seen_modules.add(entry.module_name)
                imported_modules.append(entry.module_name)
            if entry.symbol_name is not None and entry.symbol_name not in seen_symbols:
                seen_symbols.add(entry.symbol_name)
                imported_symbols.append(entry.symbol_name)

        bound_names, has_star_import = cls._extract_bound_info(entries)
        return ImportExtractionResult(
            edges=edges,
            imported_modules=imported_modules,
            imported_symbols=imported_symbols,
            bound_names=bound_names,
            has_star_import=has_star_import,
        )

    def extract(
        self,
        file_record: FileRecord,
        module_node_id: str,
        tree: ast.Module,
        *,
        lines: Sequence[str] | None = None,
    ) -> ImportExtractionResult:
        """Extract all ``imports`` edges and deduplicated module/symbol lists from ``tree``.

        Args:
            file_record: Tracked ``FileRecord`` of the source module.
            module_node_id: Root module ``SymbolNode`` identifier.
            tree: Parsed ``ast.Module`` root node.
            lines: Optional pre-split source lines for UTF-8 range offsets.

        Returns:
            Immutable ``ImportExtractionResult`` with edges, modules, and symbols.
        """
        import_nodes = [
            node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        import_nodes.sort(key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0)))

        entries: list[ImportEntry] = []
        for node in import_nodes:
            evidence_range = make_range(node, lines=lines)
            if isinstance(node, ast.Import):
                entries.extend(
                    self._entries_for_import(file_record, module_node_id, node, evidence_range)
                )
            else:
                entries.extend(
                    self._entries_for_import_from(
                        file_record,
                        module_node_id,
                        node,
                        evidence_range,
                    )
                )

        return self._aggregate_entries(entries)


__all__ = [
    "ImportExtractor",
]
