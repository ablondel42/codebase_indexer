"""Root module ``SymbolNode`` extractor service."""

import ast
from collections.abc import Sequence
from pathlib import Path

from core.models.enums import Language, SymbolKind, SymbolScope
from core.models.node import SymbolNode
from core.models.range import Position, Range
from core.utils.logging import get_logger
from parsing.services.scope import FileExtractionSession
from parsing.utils.ast_helpers import get_doc_summary

logger = get_logger("parsing.services.modules")


class ModuleExtractor:
    """Builds the root ``SymbolKind.MODULE`` ``SymbolNode`` for a parsed Python file."""

    @staticmethod
    def _compute_module_span(file_lines: Sequence[str]) -> tuple[int, int, int]:
        """Compute ``(last_line_zero_based, last_char, end_line_one_based)`` for a file.

        Args:
            file_lines: Split source lines of the Python module.

        Returns:
            Tuple ``(last_line_zero_based, last_char, end_line_one_based)``.
        """
        if file_lines:
            return len(file_lines) - 1, len(file_lines[-1]), len(file_lines)
        return 0, 0, 1

    @classmethod
    def _compute_module_ranges(cls, file_lines: Sequence[str]) -> tuple[Range, Range, int]:
        """Build ``(decl_range, selection_range, end_line_one_based)`` for module lines."""
        last_line, last_char, end_line = cls._compute_module_span(file_lines)
        zero_pos = Position(line=0, character=0)
        decl_range = Range(
            start=zero_pos,
            end=Position(line=last_line, character=last_char),
        )
        return decl_range, Range(start=zero_pos, end=zero_pos), end_line

    def extract(
        self,
        session: FileExtractionSession,
        tree: ast.Module,
        file_text: str = "",
    ) -> SymbolNode:
        """Extract the root ``SymbolKind.MODULE`` node from ``tree``.

        Args:
            session: Active per-file extraction session.
            tree: Parsed ``ast.Module`` root node.
            file_text: Optional raw source fallback when ``session.lines`` is ``None``.

        Returns:
            Constructed immutable ``SymbolNode`` representing the module.
        """
        file_rec = session.file_record
        module_path = session.module_path
        name = (
            module_path.split(".")[-1]
            if module_path
            else (Path(file_rec.file_path).stem or "__init__")
        )
        qualified_name = module_path or name
        package_path = ".".join(module_path.split(".")[:-1]) if "." in module_path else ""
        file_lines: Sequence[str] = (
            session.lines if session.lines is not None else file_text.splitlines()
        )
        decl_range, selection_range, end_line = self._compute_module_ranges(file_lines)
        node_id = session.duplicate_tracker.get_symbol_id(
            session.repo_id,
            SymbolKind.MODULE,
            qualified_name,
        )

        return SymbolNode(
            id=node_id,
            repo_id=session.repo_id,
            file_id=file_rec.id,
            language=Language.PYTHON,
            kind=SymbolKind.MODULE,
            name=name,
            qualified_name=qualified_name,
            uri=file_rec.uri,
            decl_range=decl_range,
            selection_range=selection_range,
            start_line=1,
            end_line=end_line,
            signature=None,
            parent_id=None,
            visibility_hint="module",
            doc_summary=get_doc_summary(tree),
            content_hash=file_rec.content_hash,
            semantic_hash=file_rec.content_hash,
            source="python-ast",
            confidence=1.0,
            scope=SymbolScope.MODULE,
            lexical_parent_id=None,
            payload={
                "file_path": file_rec.file_path,
                "module_path": module_path,
                "package_path": package_path,
                "imported_modules": [],
                "imported_symbols": [],
                "top_level_symbol_ids": [],
            },
            last_indexed_at=file_rec.last_indexed_at,
        )


__all__ = [
    "ModuleExtractor",
]
