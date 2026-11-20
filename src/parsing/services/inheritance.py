"""Python AST ``inherits`` edge extractor service."""

import ast
from collections.abc import Mapping, Sequence

from core.models.edge import Edge
from core.models.enums import PREFIX_EDGE, EdgeKind, SymbolKind
from core.models.file import FileRecord
from core.models.node import SymbolNode
from core.utils.logging import get_logger
from parsing.utils.ast_helpers import safe_unparse
from parsing.utils.naming import build_nested_qualified_name
from parsing.utils.ranges import make_declaration_lines, make_range

logger = get_logger("parsing.services.inheritance")

_AstClassMap = tuple[dict[tuple[str, int], ast.ClassDef], dict[str, ast.ClassDef]]


class InheritanceExtractor:
    """Extracts ``inherits`` ``Edge`` items across top-level, nested, and local classes."""

    @staticmethod
    def _make_inherits_edge_id(
        repo_id: str,
        class_node_id: str,
        placeholder_id: str,
        dup_count: int = 0,
    ) -> str:
        """Build a deterministic edge ID for an ``inherits`` edge."""
        base = f"{PREFIX_EDGE}{repo_id}:{EdgeKind.INHERITS.value}:{class_node_id}->{placeholder_id}"
        if dup_count > 0:
            return f"{base}:dup{dup_count}"
        return base

    @classmethod
    def _walk_class_defs(
        cls,
        stmts: Sequence[ast.stmt],
        module_path: str,
        chain: list[str],
        maps: _AstClassMap,
    ) -> None:
        """Recursively index ``ast.ClassDef`` nodes by ``(qualified_name, start_line)``."""
        by_qname_line, by_qname = maps
        for stmt in stmts:
            if isinstance(stmt, ast.ClassDef):
                qname = build_nested_qualified_name(module_path, chain, stmt.name)
                span = make_declaration_lines(stmt)
                by_qname_line[(qname, span.start_line)] = stmt
                by_qname.setdefault(qname, stmt)
                chain.append(stmt.name)
                cls._walk_class_defs(stmt.body, module_path, chain, maps)
                chain.pop()
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                chain.append(stmt.name)
                cls._walk_class_defs(stmt.body, module_path, chain, maps)
                chain.pop()

    def _build_class_base_edges(
        self,
        file_record: FileRecord,
        class_node_id: str,
        ast_node: ast.ClassDef,
        lines: Sequence[str] | None,
    ) -> list[Edge]:
        """Build ``inherits`` edges for a single ``ast.ClassDef`` node."""
        edges: list[Edge] = []
        seen_bases: dict[str, int] = {}

        for base in ast_node.bases:
            base_name = safe_unparse(base)
            dup_count = seen_bases.get(base_name, 0)
            seen_bases[base_name] = dup_count + 1
            placeholder_id = f"unresolved_base:{base_name}"
            edges.append(
                Edge(
                    id=self._make_inherits_edge_id(
                        file_record.repo_id,
                        class_node_id,
                        placeholder_id,
                        dup_count,
                    ),
                    repo_id=file_record.repo_id,
                    kind=EdgeKind.INHERITS,
                    from_id=class_node_id,
                    to_id=placeholder_id,
                    source="python-ast",
                    confidence=0.75,
                    evidence_file_id=file_record.id,
                    evidence_uri=file_record.uri,
                    evidence_range=make_range(base, lines=lines),
                    payload={
                        "base_name": base_name,
                        "unresolved_target": placeholder_id,
                    },
                    last_indexed_at=file_record.last_indexed_at,
                )
            )
        return edges

    def _resolve_fallback_ast_map(
        self,
        file_record: FileRecord,
        class_nodes: Sequence[SymbolNode],
        tree: ast.Module,
    ) -> dict[str, ast.ClassDef]:
        """Build a fallback ``symbol_id -> ast.ClassDef`` map when AST-ID maps are omitted."""
        by_qname_line: dict[tuple[str, int], ast.ClassDef] = {}
        by_qname: dict[str, ast.ClassDef] = {}
        self._walk_class_defs(
            tree.body,
            file_record.module_path,
            [],
            (by_qname_line, by_qname),
        )
        resolved: dict[str, ast.ClassDef] = {}
        for class_node in class_nodes:
            if class_node.kind != SymbolKind.CLASS:
                continue
            qname = class_node.qualified_name
            ast_node = (
                by_qname_line.get((qname, class_node.start_line))
                if class_node.start_line is not None
                else None
            ) or by_qname.get(qname)
            if ast_node is not None:
                resolved[class_node.id] = ast_node
        return resolved

    def extract(
        self,
        file_record: FileRecord,
        class_nodes: Sequence[SymbolNode],
        tree: ast.Module,
        *,
        lines: Sequence[str] | None = None,
        class_ast_by_symbol_id: Mapping[str, ast.ClassDef] | None = None,
    ) -> list[Edge]:
        """Extract ``inherits`` edges for all extracted class nodes in ``tree``."""
        ast_map = (
            class_ast_by_symbol_id
            if class_ast_by_symbol_id is not None
            else self._resolve_fallback_ast_map(file_record, class_nodes, tree)
        )
        edges: list[Edge] = []
        for class_node in class_nodes:
            if class_node.kind != SymbolKind.CLASS:
                continue
            ast_node = ast_map.get(class_node.id)
            if ast_node is None:
                continue
            edges.extend(self._build_class_base_edges(file_record, class_node.id, ast_node, lines))
        return edges


__all__ = [
    "InheritanceExtractor",
]
