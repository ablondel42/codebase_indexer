"""Immutable configuration and diagnostic helper models for SQLite storage."""

from pathlib import Path
from typing import Any

from pydantic import Field, field_validator

from core.models.base import FrozenModel
from core.models.diagnostics import LspCoverageMetrics
from core.models.repo import RepoRecord
from core.models.runs import IndexRunRecord


class ConnectionOptions(FrozenModel):
    """Configuration options for opening a SQLite connection via ``ConnectionFactory``."""

    db_path: Path
    read_only: bool = False
    must_exist: bool = False
    with_select_authorizer: bool = False
    sqlite_uri: str | None = None

    @field_validator("db_path", mode="before")
    @classmethod
    def _coerce_db_path(cls, value: Any) -> Any:
        """Coerce string paths (including ``':memory:'``) into ``Path`` instances.

        Args:
            value: Candidate database path value (``str`` or ``Path``).

        Returns:
            Validated ``Path`` instance (``Path(":memory:")`` for in-memory SQLite).

        Raises:
            ValueError: If ``value`` is an empty or whitespace-only string/path.
        """
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("db_path must not be empty")
            return Path(value)
        if isinstance(value, Path) and not str(value).strip():
            raise ValueError("db_path must not be empty")
        return value

    @property
    def is_memory(self) -> bool:
        """Return ``True`` when ``db_path`` targets an in-memory SQLite database.

        Returns:
            ``True`` if ``str(self.db_path) == ":memory:"``, otherwise ``False``.
        """
        return str(self.db_path) == ":memory:"


class SchemaStatusMetrics(FrozenModel):
    """Internal aggregated metrics collected when inspecting a v2 SQLite database."""

    all_repos: list[RepoRecord] = Field(default_factory=list)
    selected_repo: RepoRecord | None = None
    edge_counts_by_kind: dict[str, int] = Field(default_factory=dict)
    lsp_coverage: LspCoverageMetrics = Field(default_factory=LspCoverageMetrics)
    latest_run: IndexRunRecord | None = None
