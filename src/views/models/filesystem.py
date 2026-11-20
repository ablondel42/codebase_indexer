"""Pydantic v2 models for the repository filesystem Explorer tree view."""

from pydantic import Field

from core.models.base import FrozenModel
from core.models.search import TextDocumentMeta


class FolderNode(FrozenModel):
    """Directory or codebase root node holding sorted subfolders and indexed files."""

    name: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    folders: tuple["FolderNode", ...] = ()
    files: tuple[TextDocumentMeta, ...] = ()


FolderNode.model_rebuild()


class FilesystemViewQuery(FrozenModel):
    """Filter parameters for building a repository filesystem tree view."""

    repo: str | None = None
    path_prefix: str | None = None
    include_tests: bool = True
    max_depth: int | None = Field(default=None, ge=0)


class FilesystemView(FrozenModel):
    """Explorer tree view of an indexed repository's folders and files."""

    repo_id: str
    repo_name: str
    root: FolderNode


__all__ = [
    "FilesystemView",
    "FilesystemViewQuery",
    "FolderNode",
]
