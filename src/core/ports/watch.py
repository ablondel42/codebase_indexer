"""Port contract for raw filesystem directory watching across domains."""

from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol, TypeAlias, runtime_checkable

WatchEventKind: TypeAlias = Literal["created", "modified", "deleted", "moved"]
PathChangeCallback: TypeAlias = Callable[[WatchEventKind, Path, bool], None]


@runtime_checkable
class DirectoryWatchHandleProtocol(Protocol):
    """Handle controlling an active multi-directory filesystem observer."""

    def add_watch(self, path: Path, *, recursive: bool = False) -> bool:
        """Schedule watching ``path`` if it exists and is not yet watched."""
        ...

    def stop(self) -> None:
        """Stop and join the underlying filesystem observer cleanly."""
        ...


__all__ = [
    "DirectoryWatchHandleProtocol",
    "PathChangeCallback",
    "WatchEventKind",
]
