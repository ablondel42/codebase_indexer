"""Pydantic v2 models for LSP target document preparation and symbol enrichment results."""

from pathlib import Path

from pydantic import Field

from core.models.base import FrozenModel, MutableModel
from core.models.enums import RefreshStatus
from core.models.file import FileRecord
from core.models.node import SymbolNode
from core.models.range import Position


class TargetDocumentSpec(FrozenModel):
    """Validated target document text, lines, URI, and UTF-16 query position."""

    repo_root: Path
    target_file_path: Path
    target_uri: str
    target_text: str
    target_lines: list[str]
    utf16_position: Position


class SymbolEnrichmentResult(FrozenModel):
    """Outcome and reference fanout statistics for a single-symbol LSP refresh."""

    target_symbol_id: str
    available: bool
    reference_count: int = 0
    referencing_file_count: int = 0
    referencing_module_count: int = 0
    last_refreshed_at: str | None = None
    refresh_status: RefreshStatus
    error_code: str | None = None
    error_message: str | None = None


class ReferenceMappingCache(MutableModel):
    """Per-enrichment in-memory lookup and deduplication state for reference mapping."""

    repo_id: str
    repo_root: Path
    max_file_size_bytes: int
    target_symbol: SymbolNode
    file_lines: dict[str, list[str] | None] = Field(default_factory=dict)
    seen_sites: set[tuple[str, str, str, int, int]] = Field(default_factory=set)
    files_by_uri: dict[str, FileRecord | None] = Field(default_factory=dict)
    symbols_by_file: dict[str, list[SymbolNode]] = Field(default_factory=dict)
