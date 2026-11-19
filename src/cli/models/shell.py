"""Mutable session state model for the interactive ``rc>`` shell."""

from pathlib import Path

from core.models.base import MutableModel


class ShellSessionState(MutableModel):
    """Mutable session context persisted across interactive ``rc>`` REPL commands."""

    db_path: Path
    active_repo_id: str | None = None


__all__ = [
    "ShellSessionState",
]
