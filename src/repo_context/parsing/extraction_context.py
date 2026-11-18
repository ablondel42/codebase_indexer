"""Shared extraction context and scope target dataclasses for AST extractors."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from repo_context.models.file import FileRecord
from repo_context.parsing.naming import DuplicateTracker
from repo_context.parsing.scope_tracker import ScopeTracker


@dataclass(frozen=True)
class ExtractionContext:
    """Shared per-file state passed across AST symbol extractors."""

    repo_id: str
    file_record: FileRecord
    duplicate_tracker: DuplicateTracker = field(default_factory=DuplicateTracker)
    scope_tracker: ScopeTracker = field(default_factory=ScopeTracker)
    lines: Sequence[str] | None = None

    @property
    def module_path(self) -> str:
        """Return the Python dotted module path for the current file."""
        return self.file_record.module_path


@dataclass(frozen=True)
class ScopeTarget:
    """Structural parent and scope target for extracted AST declarations."""

    parent_id: str
    parent_qualified_name: str
    scope: str = "module"
