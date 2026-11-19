"""Indexing run audit records, completion metrics, and LSP refresh state models."""

from core.models.base import FrozenModel


class IndexRunRecord(FrozenModel):
    """Persisted audit record for a full or incremental repository indexing run."""

    id: str
    repo_id: str
    run_type: str
    started_at: str
    completed_at: str | None = None
    status: str
    files_scanned: int = 0
    nodes_extracted: int = 0
    edges_extracted: int = 0
    error_message: str | None = None


class IndexRunMetrics(FrozenModel):
    """Completion counters and optional error metadata when finishing an index run."""

    files_scanned: int = 0
    nodes_extracted: int = 0
    edges_extracted: int = 0
    error_message: str | None = None
    completed_at: str | None = None


class ReferenceRefreshState(FrozenModel):
    """Persisted LSP reference refresh state for a target symbol."""

    target_symbol_id: str
    available: bool = False
    last_refreshed_at: str | None = None
    refresh_status: str | None = None
    error_code: str | None = None
