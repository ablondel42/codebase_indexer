"""Per-file symbol duplicate disambiguation, lexical scope stack, and extraction session."""

from collections.abc import Generator, Sequence
from contextlib import contextmanager
from typing import Self

from core.models.enums import PREFIX_SYMBOL, SymbolKind, SymbolScope
from core.models.file import FileRecord
from core.utils.logging import get_logger
from parsing.models.extraction import ScopeFrame

logger = get_logger("parsing.services.scope")


class DuplicateTracker:
    """Disambiguates duplicate ``(kind, qualified_name)`` declarations within a file."""

    def __init__(self) -> None:
        """Initialize an empty per-file ``(kind, qualified_name)`` counter map."""
        self._seen: dict[tuple[str, str], int] = {}

    def get_symbol_id(
        self,
        repo_id: str,
        kind: SymbolKind | str,
        qualified_name: str,
    ) -> str:
        """Allocate a deterministic symbol ID, appending ``:dup{N}`` on collisions.

        Args:
            repo_id: Owning repository identifier.
            kind: Symbol declaration kind.
            qualified_name: Full dotted symbol qualified name.

        Returns:
            Unique symbol identifier within the current file extraction.
        """
        kind_val = kind.value if isinstance(kind, SymbolKind) else str(kind)
        key = (kind_val, qualified_name)
        count = self._seen.get(key, 0)
        self._seen[key] = count + 1

        base_id = f"{PREFIX_SYMBOL}{repo_id}:{kind_val}:{qualified_name}"
        if count > 0:
            logger.debug("Disambiguated duplicate symbol %s as :dup%d", base_id, count)
            return f"{base_id}:dup{count}"
        return base_id


class ScopeTracker:
    """Maintains the active lexical declaration stack during per-file AST traversal."""

    def __init__(self) -> None:
        """Initialize an empty ``ScopeFrame`` stack."""
        self._stack: list[ScopeFrame] = []

    def push_declaration(
        self,
        symbol_id: str,
        name: str,
        scope: SymbolScope | str,
    ) -> None:
        """Push an active declaration frame onto the scope stack.

        Args:
            symbol_id: Enclosing symbol's deterministic ID.
            name: Simple declared name.
            scope: Enclosing ``SymbolScope`` (``module``, ``class``, or ``function``).
        """
        resolved_scope = scope if isinstance(scope, SymbolScope) else SymbolScope(scope)
        self._stack.append(ScopeFrame(symbol_id=symbol_id, name=name, scope=resolved_scope))

    def pop_declaration(self) -> ScopeFrame | None:
        """Pop and return the innermost ``ScopeFrame`` if non-empty.

        Returns:
            Popped ``ScopeFrame``, or ``None`` if the stack was already empty.
        """
        if self._stack:
            return self._stack.pop()
        return None

    def get_current_scope(self) -> SymbolScope:
        """Return the innermost active ``SymbolScope`` (defaulting to ``MODULE``).

        Returns:
            Current ``SymbolScope`` value.
        """
        if not self._stack:
            return SymbolScope.MODULE
        return self._stack[-1].scope

    def get_lexical_parent_id(self) -> str | None:
        """Return the immediate enclosing symbol ID, or ``None`` at module top level.

        Returns:
            Innermost frame's ``symbol_id`` or ``None``.
        """
        if not self._stack:
            return None
        return self._stack[-1].symbol_id

    def get_lexical_chain(self) -> list[str]:
        """Return enclosing declaration names ordered from outermost to innermost.

        Returns:
            List of simple declaration names on the stack.
        """
        return [frame.name for frame in self._stack]

    def is_empty(self) -> bool:
        """Return ``True`` when no declarations are pushed onto the stack.

        Returns:
            ``True`` if stack depth is zero.
        """
        return len(self._stack) == 0

    @contextmanager
    def scope_context(
        self,
        symbol_id: str,
        name: str,
        scope: SymbolScope | str,
    ) -> Generator[Self, None, None]:
        """Push a declaration frame on entry and pop it symmetrically on exit.

        Args:
            symbol_id: Enclosing symbol's deterministic ID.
            name: Simple declared name.
            scope: Enclosing ``SymbolScope`` value.

        Yields:
            Active ``ScopeTracker`` instance.
        """
        self.push_declaration(symbol_id, name, scope)
        try:
            yield self
        finally:
            self.pop_declaration()


class FileExtractionSession:
    """Shared per-file state bundling source lines, ``DuplicateTracker``, and ``ScopeTracker``."""

    def __init__(
        self,
        file_record: FileRecord,
        lines: Sequence[str] | None = None,
        *,
        duplicate_tracker: DuplicateTracker | None = None,
        scope_tracker: ScopeTracker | None = None,
    ) -> None:
        """Initialize a single-file AST extraction session.

        Args:
            file_record: Tracked ``FileRecord`` being extracted.
            lines: Optional pre-split source lines of the file.
            duplicate_tracker: Optional injected ``DuplicateTracker`` instance.
            scope_tracker: Optional injected ``ScopeTracker`` instance.
        """
        self.file_record = file_record
        self.lines: tuple[str, ...] | None = tuple(lines) if lines is not None else None
        self.duplicate_tracker = (
            duplicate_tracker if duplicate_tracker is not None else DuplicateTracker()
        )
        self.scope_tracker = scope_tracker if scope_tracker is not None else ScopeTracker()

    @property
    def repo_id(self) -> str:
        """Return the owning repository identifier from ``file_record``.

        Returns:
            Repository identifier string.
        """
        return self.file_record.repo_id

    @property
    def module_path(self) -> str:
        """Return the dotted Python module path from ``file_record``.

        Returns:
            Dotted Python module path string.
        """
        return self.file_record.module_path


__all__ = [
    "DuplicateTracker",
    "FileExtractionSession",
    "ScopeTracker",
]
