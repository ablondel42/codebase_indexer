"""Per-file symbol duplicate disambiguation, lexical scope stack, and extraction session."""

from collections.abc import Sequence
from types import TracebackType

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


class ScopeContext:
    """Context manager keeping ``ScopeTracker`` push/pop operations balanced."""

    def __init__(
        self,
        tracker: "ScopeTracker",
        symbol_id: str,
        name: str,
        scope: SymbolScope,
    ) -> None:
        """Initialize a balanced scope context frame.

        Args:
            tracker: Owning ``ScopeTracker`` stack.
            symbol_id: Enclosing declaration symbol identifier.
            name: Simple declared identifier name.
            scope: Enclosing ``SymbolScope`` kind.
        """
        self._tracker = tracker
        self._symbol_id = symbol_id
        self._name = name
        self._scope = scope

    def __enter__(self) -> "ScopeTracker":
        """Push the declaration frame onto ``ScopeTracker`` on block entry.

        Returns:
            Active ``ScopeTracker`` instance.
        """
        self._tracker.push_declaration(self._symbol_id, self._name, self._scope)
        return self._tracker

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Pop the declaration frame from ``ScopeTracker`` without suppressing errors.

        Args:
            exc_type: Exception class if raised inside the ``with`` block, else ``None``.
            exc_val: Exception instance if raised, else ``None``.
            exc_tb: Traceback object if raised, else ``None``.
        """
        self._tracker.pop_declaration()


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

    def get_lexical_chain_ids(self) -> list[str]:
        """Return enclosing declaration symbol IDs ordered from outermost to innermost.

        Returns:
            List of ``symbol_id`` values on the stack.
        """
        return [frame.symbol_id for frame in self._stack]

    def is_empty(self) -> bool:
        """Return ``True`` when no declarations are pushed onto the stack.

        Returns:
            ``True`` if stack depth is zero.
        """
        return len(self._stack) == 0

    def depth(self) -> int:
        """Return the number of active frames on the scope stack.

        Returns:
            Current integer stack depth.
        """
        return len(self._stack)

    def scope_context(
        self,
        symbol_id: str,
        name: str,
        scope: SymbolScope | str,
    ) -> ScopeContext:
        """Create a context manager that pushes on entry and pops on exit.

        Args:
            symbol_id: Enclosing symbol's deterministic ID.
            name: Simple declared name.
            scope: Enclosing ``SymbolScope`` value.

        Returns:
            ``ScopeContext`` bound to this tracker.
        """
        resolved_scope = scope if isinstance(scope, SymbolScope) else SymbolScope(scope)
        return ScopeContext(self, symbol_id, name, resolved_scope)


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
    "ScopeContext",
    "ScopeTracker",
]
