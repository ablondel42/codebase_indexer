"""Source snippet options, sliced source windows, and UI-ready symbol snippet models."""

from pydantic import Field

from core.models.base import FrozenModel
from core.models.enums import SymbolKind
from core.models.range import Range


class SnippetOptions(FrozenModel):
    """Line-window pagination, context padding, and hash verification options."""

    max_lines: int = Field(default=120, ge=1)
    offset: int = Field(default=0, ge=0)
    padding_lines: int = Field(default=0, ge=0)
    expected_content_hash: str | None = None


class SourceWindow(FrozenModel):
    """Computed 1-based source window carrying both un-numbered and numbered text."""

    start_line: int
    end_line: int
    window_start_line: int
    window_end_line: int
    raw_source: str
    numbered_source: str
    truncated: bool
    total_lines: int
    returned_lines: int
    next_offset: int | None = None


class SourceSlice(FrozenModel):
    """Verified file source slice with raw text, line-numbered text, and freshness."""

    file_path: str
    start_line: int
    end_line: int
    window_start_line: int
    window_end_line: int
    raw_source: str
    numbered_source: str
    truncated: bool
    total_lines: int
    returned_lines: int
    next_offset: int | None = None
    is_fresh: bool = True
    stale_reason: str | None = None


class SymbolSnippet(FrozenModel):
    """UI-ready and CLI/MCP-ready symbol source snippet with range and freshness metadata."""

    symbol_id: str
    name: str
    qualified_name: str
    kind: SymbolKind
    location: str | None = None
    file_path: str
    module_path: str
    language: str = "python"
    signature: str | None = None
    doc_summary: str | None = None
    start_line: int
    end_line: int
    decl_range: Range | None = None
    selection_range: Range | None = None
    lines: str
    window_start_line: int
    window_end_line: int
    raw_source: str
    numbered_source: str
    truncated: bool
    total_lines: int
    returned_lines: int
    next_offset: int | None = None
    is_fresh: bool = True
    stale_reason: str | None = None


__all__ = [
    "SnippetOptions",
    "SourceSlice",
    "SourceWindow",
    "SymbolSnippet",
]
