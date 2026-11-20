"""Stateless bottom-up filesystem tree construction helpers for ``views``."""

from collections.abc import Sequence

from core.models.search import TextDocumentMeta
from views.models.filesystem import FolderNode


def _relative_dir_depth(dir_key: str, root_dir_key: str) -> int:
    """Return 0-based nesting depth of ``dir_key`` relative to ``root_dir_key``."""
    if dir_key == root_dir_key:
        return 0
    if not root_dir_key:
        return dir_key.count("/") + 1
    suffix = dir_key.removeprefix(f"{root_dir_key}/")
    return suffix.count("/") + 1


def _register_parent_chain(
    parent_dir: str,
    root_dir_key: str,
    all_dirs: set[str],
    child_dirs: dict[str, set[str]],
) -> None:
    """Register ``parent_dir`` and all ancestor directories up to ``root_dir_key``."""
    cur = parent_dir
    while cur and cur != root_dir_key:
        all_dirs.add(cur)
        up = cur.rsplit("/", 1)[0] if "/" in cur else ""
        child_dirs.setdefault(up, set()).add(cur)
        cur = up


def _index_directory_entries(
    docs: Sequence[TextDocumentMeta],
    root_dir_key: str,
) -> tuple[set[str], dict[str, set[str]], dict[str, list[TextDocumentMeta]]]:
    """Group ``docs`` by parent directory and collect child-directory adjacency."""
    all_dirs: set[str] = {root_dir_key}
    child_dirs: dict[str, set[str]] = {}
    files_by_dir: dict[str, list[TextDocumentMeta]] = {}
    for doc in docs:
        parent = doc.file_path.rsplit("/", 1)[0] if "/" in doc.file_path else ""
        files_by_dir.setdefault(parent, []).append(doc)
        _register_parent_chain(parent, root_dir_key, all_dirs, child_dirs)
    return all_dirs, child_dirs, files_by_dir


def _file_sort_key(doc: TextDocumentMeta) -> tuple[str, str]:
    """Return case-insensitive basename sort key for Explorer file ordering."""
    fname = doc.file_path.rsplit("/", 1)[-1]
    return (fname.casefold(), fname)


def _assemble_folder_node(
    dir_key: str,
    root_name: str,
    subfolders: tuple[FolderNode, ...],
    direct_files: tuple[TextDocumentMeta, ...],
) -> FolderNode:
    """Instantiate one immutable ``FolderNode`` for ``dir_key``."""
    name = root_name if not dir_key else dir_key.rsplit("/", 1)[-1]
    path = dir_key or "."
    return FolderNode(
        name=name,
        path=path,
        folders=subfolders,
        files=direct_files,
    )


def build_folder_tree(
    root_name: str,
    docs: Sequence[TextDocumentMeta],
    *,
    focus_prefix: str = "",
    max_depth: int | None = None,
) -> FolderNode:
    """Build an Explorer-ordered ``FolderNode`` tree bottom-up from indexed file metadata.

    Subfolders appear before files at every level, each sorted case-insensitively.

    Args:
        root_name: Display name used when rendering the repository root node.
        docs: Filtered ``TextDocumentMeta`` entries belonging to the target subtree.
        focus_prefix: Normalized relative directory prefix (``""`` for repository root).
        max_depth: Optional 0-based depth limit relative to ``focus_prefix``.

    Returns:
        Root ``FolderNode`` containing nested ``folders`` and ``files``.
    """
    all_dirs, child_dirs, files_by_dir = _index_directory_entries(docs, focus_prefix)
    ordered_dirs = sorted(
        all_dirs,
        key=lambda d: _relative_dir_depth(d, focus_prefix),
        reverse=True,
    )
    built: dict[str, FolderNode] = {}
    for dir_key in ordered_dirs:
        depth = _relative_dir_depth(dir_key, focus_prefix)
        if max_depth is not None and depth >= max_depth:
            subfolders: tuple[FolderNode, ...] = ()
            direct_files: tuple[TextDocumentMeta, ...] = ()
        else:
            subfolders = tuple(
                sorted(
                    (built[child_key] for child_key in child_dirs.get(dir_key, ())),
                    key=lambda node: (node.name.casefold(), node.name),
                )
            )
            direct_files = tuple(sorted(files_by_dir.get(dir_key, ()), key=_file_sort_key))
        built[dir_key] = _assemble_folder_node(dir_key, root_name, subfolders, direct_files)
    return built[focus_prefix]


__all__ = [
    "build_folder_tree",
]
