"""AST symbol and edge extraction value objects, metrics, and file graph models."""

from typing import Any

from core.models.base import FrozenModel
from core.models.edge import Edge
from core.models.enums import SymbolKind, SymbolScope
from core.models.file import FileRecord
from core.models.node import SymbolNode
from core.models.range import Range


class ScopeFrame(FrozenModel):
    """Single active declaration frame pushed onto a per-file ``ScopeTracker``."""

    symbol_id: str
    name: str
    scope: SymbolScope


class ScopeTarget(FrozenModel):
    """Structural and lexical parent target for declarations inside a scope."""

    parent_id: str
    parent_qualified_name: str
    scope: SymbolScope = SymbolScope.MODULE
    lexical_parent_id: str | None = None


class DeclarationPlacement(FrozenModel):
    """Resolved structural/lexical parent IDs, kind, scope, and qualified name."""

    kind: SymbolKind
    parent_id: str | None = None
    lexical_parent_id: str | None = None
    qualified_name: str
    scope: SymbolScope


class ParameterSpec(FrozenModel):
    """Normalized metadata for a single callable parameter."""

    name: str
    kind: str
    annotation: str | None = None
    default_value_hint: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Serialize parameter metadata for ``SymbolNode.payload['parameters']``.

        Returns:
            Dictionary omitting ``default_value_hint`` when no default expression exists.
        """
        data: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "annotation": self.annotation,
        }
        if self.default_value_hint is not None:
            data["default_value_hint"] = self.default_value_hint
        return data


class AssignmentSpec(FrozenModel):
    """Extracted metadata for a single constant, class attribute, or type alias."""

    kind: SymbolKind
    name: str
    signature: str
    start_line: int
    end_line: int
    decl_range: Range | None = None
    selection_range: Range | None = None
    content_hash: str = ""
    annotation: str | None = None
    value_hint: str | None = None


class ImportEntry(FrozenModel):
    """Single extracted import alias entry pairing its ``Edge`` with module/symbol names."""

    edge: Edge
    module_name: str = ""
    symbol_name: str | None = None


class ImportExtractionResult(FrozenModel):
    """Aggregated ``imports`` edges and deduplicated module/symbol lists for one file."""

    edges: list[Edge]
    imported_modules: list[str]
    imported_symbols: list[str]


class DeclarationWalkResult(FrozenModel):
    """Ordered symbol declarations, extracted class nodes, and structural edges."""

    nodes: list[SymbolNode]
    class_nodes: list[SymbolNode]
    structural_edges: list[Edge]


class ExtractionMetrics(FrozenModel):
    """Per-file symbol and edge extraction summary counts."""

    file_id: str
    file_path: str
    node_count: int
    edge_count: int
    module_count: int = 1
    class_count: int = 0
    callable_count: int = 0
    method_count: int = 0
    constant_count: int = 0
    attribute_count: int = 0
    type_alias_count: int = 0
    call_edge_count: int = 0


class FileGraphExtraction(FrozenModel):
    """Complete AST extraction output for a single tracked source file."""

    file: FileRecord
    nodes: list[SymbolNode]
    edges: list[Edge]
    metrics: ExtractionMetrics


__all__ = [
    "AssignmentSpec",
    "DeclarationPlacement",
    "DeclarationWalkResult",
    "ExtractionMetrics",
    "FileGraphExtraction",
    "ImportEntry",
    "ImportExtractionResult",
    "ParameterSpec",
    "ScopeFrame",
    "ScopeTarget",
]
