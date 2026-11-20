"""Leaf ``SymbolKind.CLASS`` node extractor service."""

import ast
from collections.abc import Sequence

from core.models.enums import SymbolKind
from core.models.node import SymbolNode
from core.utils.hashing import compute_symbol_content_hash
from core.utils.logging import get_logger
from parsing.models.extraction import DeclarationPlacement, ScopeTarget
from parsing.services.scope import FileExtractionSession
from parsing.utils.ast_helpers import (
    cap_signature,
    get_doc_summary,
    safe_unparse,
)
from parsing.utils.naming import (
    build_symbol_node,
    compute_semantic_digest,
    resolve_symbol_placement,
)
from parsing.utils.ranges import (
    make_declaration_lines,
    make_name_selection_range,
    make_range,
)

logger = get_logger("parsing.services.classes")


class ClassExtractor:
    """Leaf builder for single ``SymbolKind.CLASS`` ``SymbolNode`` declarations."""

    @staticmethod
    def _build_signature(class_name: str, base_names: Sequence[str]) -> str:
        """Build a compact single-line class signature capped at 240 characters."""
        raw = (
            f"class {class_name}({', '.join(base_names)})" if base_names else f"class {class_name}"
        )
        return cap_signature(raw)

    @staticmethod
    def _compute_semantic_hash(
        qualified_name: str,
        base_names: list[str],
        decorators: list[str],
    ) -> str:
        """Compute a deterministic semantic hash for a class declaration."""
        return compute_semantic_digest((
            "kind:class",
            f"qualified_name:{qualified_name}",
            f"bases:{base_names}",
            f"decorators:{decorators}",
        ))

    def resolve_placement(
        self,
        session: FileExtractionSession,
        class_name: str,
        module_node_id: str | None = None,
        target: ScopeTarget | None = None,
    ) -> DeclarationPlacement:
        """Resolve structural/lexical parent IDs, scope, and qualified name for a class."""
        tracker = session.scope_tracker
        return resolve_symbol_placement(
            session.module_path,
            tracker.get_lexical_chain(),
            (tracker.get_current_scope(), tracker.get_lexical_parent_id()),
            kind=SymbolKind.CLASS,
            name=class_name,
            target=(target, module_node_id),
        )

    def extract(
        self,
        session: FileExtractionSession,
        node: ast.ClassDef,
        node_id: str,
        placement: DeclarationPlacement,
        method_ids: Sequence[str] = (),
    ) -> SymbolNode:
        """Build an immutable ``SymbolNode`` for a single ``ast.ClassDef`` without recursion."""
        base_names = [safe_unparse(base) for base in node.bases]
        decorators = [safe_unparse(decorator) for decorator in node.decorator_list]
        span = make_declaration_lines(node)
        content_hash = compute_symbol_content_hash(
            node,
            session.lines,
            span.start_line,
            span.end_line,
        )
        semantic_hash = self._compute_semantic_hash(
            placement.qualified_name,
            base_names,
            decorators,
        )
        return build_symbol_node(
            session.file_record,
            node_id,
            node.name,
            placement,
            spans=(
                make_range(node, lines=session.lines),
                make_name_selection_range(node, lines=session.lines),
                span.start_line,
                span.end_line,
            ),
            metadata=(
                self._build_signature(node.name, base_names),
                get_doc_summary(node),
                content_hash,
                semantic_hash,
                {
                    "base_names": base_names,
                    "decorators": decorators,
                    "method_ids": list(method_ids),
                },
            ),
        )


__all__ = [
    "ClassExtractor",
]
