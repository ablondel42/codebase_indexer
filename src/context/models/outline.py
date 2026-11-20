"""Hierarchical file symbol outline models."""

from pydantic import Field

from core.models.base import FrozenModel
from core.models.enums import SymbolKind


class OutlineNode(FrozenModel):
    """Recursive symbol declaration node in a hierarchical file outline."""

    id: str
    kind: SymbolKind
    name: str
    location: str | None = None
    lines: str | None = None
    signature: str | None = None
    doc: str | None = None
    children: list["OutlineNode"] = Field(default_factory=list)


OutlineNode.model_rebuild()


class FileOutline(FrozenModel):
    """Depth-bounded symbol skeleton, docstring, imports, and freshness for one file."""

    file_path: str
    location: str | None = None
    module_path: str
    line_count: int
    is_fresh: bool
    stale_reason: str | None = None
    doc: str | None = None
    imports: list[str] = Field(default_factory=list)
    symbols: list[OutlineNode] = Field(default_factory=list)


__all__ = [
    "FileOutline",
    "OutlineNode",
]
