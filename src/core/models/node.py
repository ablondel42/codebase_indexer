"""Symbol declaration node, UI-ready symbol summary, and search/pagination DTOs."""

from typing import Any, Self

from pydantic import Field, field_validator

from core.models.base import FrozenModel
from core.models.enums import Language, SymbolKind, SymbolScope
from core.models.range import Range
from core.utils.timestamps import validate_iso_timestamp


class SymbolNode(FrozenModel):
    """Canonical symbol declaration node in the repository graph."""

    id: str = Field(..., min_length=1, pattern=r"^sym:.+")
    repo_id: str = Field(..., min_length=1, pattern=r"^repo:.+")
    file_id: str = Field(..., min_length=1, pattern=r"^file:.+")
    language: str = Field(default=Language.PYTHON)
    kind: SymbolKind
    name: str = Field(..., min_length=1, max_length=1024)
    qualified_name: str = Field(..., min_length=1)
    uri: str = Field(..., pattern=r"^file://")
    content_hash: str = ""
    semantic_hash: str = ""
    source: str = "python-ast"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    payload: dict[str, Any] = Field(default_factory=dict)
    decl_range: Range | None = None
    selection_range: Range | None = None
    start_line: int | None = None
    end_line: int | None = None
    signature: str | None = None
    parent_id: str | None = None
    visibility_hint: str | None = None
    doc_summary: str | None = None
    scope: SymbolScope | None = None
    lexical_parent_id: str | None = None
    last_indexed_at: str | None = None

    @field_validator("last_indexed_at")
    @classmethod
    def _check_timestamp(cls, value: str | None) -> str | None:
        """Validate ``last_indexed_at`` is an ISO 8601 timestamp when provided.

        Args:
            value: Candidate timestamp string or ``None``.

        Returns:
            Validated timestamp string or ``None``.

        Raises:
            ValueError: If ``value`` is not a valid ISO 8601 string.
        """
        return validate_iso_timestamp(value)


class SymbolSummary(FrozenModel):
    """UI-ready symbol projection pairing node metadata with file and module paths."""

    id: str
    repo_id: str
    file_id: str
    kind: SymbolKind
    name: str
    qualified_name: str
    uri: str
    language: str = Language.PYTHON
    signature: str | None = None
    doc_summary: str | None = None
    visibility_hint: str | None = None
    scope: SymbolScope | None = None
    parent_id: str | None = None
    lexical_parent_id: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    decl_range: Range | None = None
    selection_range: Range | None = None
    confidence: float = 1.0
    content_hash: str = ""
    last_indexed_at: str | None = None
    file_path: str = ""
    module_path: str = ""
    is_test: bool = False

    @classmethod
    def from_node(
        cls,
        node: SymbolNode,
        file_path: str = "",
        module_path: str = "",
        *,
        is_test: bool = False,
    ) -> Self:
        """Build a ``SymbolSummary`` from a ``SymbolNode`` and containing file metadata.

        Args:
            node: Source ``SymbolNode`` entity.
            file_path: Repository-relative POSIX file path (default ``""``).
            module_path: Dotted Python module path (default ``""``).
            is_test: Whether the containing file is classified as a test file.

        Returns:
            Populated ``SymbolSummary`` instance.
        """
        return cls(
            id=node.id,
            repo_id=node.repo_id,
            file_id=node.file_id,
            kind=node.kind,
            name=node.name,
            qualified_name=node.qualified_name,
            uri=node.uri,
            language=node.language,
            signature=node.signature,
            doc_summary=node.doc_summary,
            visibility_hint=node.visibility_hint,
            scope=node.scope,
            parent_id=node.parent_id,
            lexical_parent_id=node.lexical_parent_id,
            start_line=node.start_line,
            end_line=node.end_line,
            decl_range=node.decl_range,
            selection_range=node.selection_range,
            confidence=node.confidence,
            content_hash=node.content_hash,
            last_indexed_at=node.last_indexed_at,
            file_path=file_path,
            module_path=module_path,
            is_test=is_test,
        )


class SymbolSearchQuery(FrozenModel):
    """Parameters for full-text or pattern symbol search."""

    repo_id: str
    query: str
    kind: SymbolKind | None = None
    file_path: str | None = None
    limit: int = Field(default=20, ge=1)


class SymbolPageQuery(FrozenModel):
    """Pagination and filter parameters for listing symbols in a repository."""

    repo_id: str
    kind: SymbolKind | None = None
    file_path: str | None = None
    limit: int = Field(default=50, ge=1)
    offset: int = Field(default=0, ge=0)


class SymbolPageResult(FrozenModel):
    """Paginated page of ``SymbolSummary`` items with total matching count."""

    total_count: int
    items: list[SymbolSummary]
