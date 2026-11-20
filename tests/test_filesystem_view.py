"""Unit and integration tests for ``src/views/`` and ``IndexerEngine.views``."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from core.models.config import AppConfig
from core.models.exceptions import ValidationError
from core.models.search import TextDocumentMeta
from engine import (
    FilesystemView,
    FilesystemViewQuery,
    FolderNode,
    IndexerEngine,
)
from indexing import BatchIndexOptions


@pytest.fixture
def indexed_view_repo(tmp_path: Path) -> Iterator[tuple[IndexerEngine, Path, Path]]:
    """Create and index a multi-folder sample codebase for ``FilesystemView`` tests."""
    repo_root = tmp_path / "ExplorerSample"
    (repo_root / "Docs").mkdir(parents=True)
    (repo_root / "src" / "Zeta").mkdir(parents=True)
    (repo_root / "src" / "alpha").mkdir(parents=True)
    (repo_root / "tests" / "unit").mkdir(parents=True)

    (repo_root / "README.md").write_text("# ExplorerSample\n\nOverview.\n", encoding="utf-8")
    (repo_root / "pyproject.toml").write_text('[project]\nname = "sample"\n', encoding="utf-8")
    (repo_root / "Zebra.txt").write_text("root zebra\n", encoding="utf-8")
    (repo_root / "Docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (repo_root / "src" / "app.py").write_text(
        "def main() -> int:\n    return 0\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "Bootstrap.py").write_text("FLAG = True\n", encoding="utf-8")
    (repo_root / "src" / "alpha" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo_root / "src" / "Zeta" / "runner.py").write_text(
        "def run() -> None:\n    pass\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "unit" / "test_app.py").write_text(
        "def test_ok() -> None:\n    assert True\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "views.db"
    engine = IndexerEngine(AppConfig(db_path=db_path, include_tests=True))
    engine.commands.index_repository(repo_root, BatchIndexOptions(include_tests=True))
    try:
        yield engine, repo_root, db_path
    finally:
        engine.close()


def _flatten_files(folder: FolderNode) -> list[TextDocumentMeta]:
    """Collect all ``TextDocumentMeta`` entries under ``folder`` in tree order."""
    items: list[TextDocumentMeta] = list(folder.files)
    for sub in folder.folders:
        items.extend(_flatten_files(sub))
    return items


def test_filesystem_view_explorer_order_and_text_meta_reuse(
    indexed_view_repo: tuple[IndexerEngine, Path, Path],
) -> None:
    """Folders sort case-insensitively before files and reuse ``TextDocumentMeta`` directly."""
    engine, _, _ = indexed_view_repo

    view = engine.views.get_filesystem_view()
    assert isinstance(view, FilesystemView)
    assert view.repo_name == "ExplorerSample"
    assert view.root.name == "ExplorerSample"
    assert view.root.path == "."

    assert [sub.name for sub in view.root.folders] == ["Docs", "src", "tests"]
    assert [doc.file_path for doc in view.root.files] == [
        "pyproject.toml",
        "README.md",
        "Zebra.txt",
    ]
    assert all(isinstance(doc, TextDocumentMeta) for doc in _flatten_files(view.root))

    readme_doc = next(doc for doc in view.root.files if doc.file_path == "README.md")
    assert readme_doc.language == "markdown"
    assert readme_doc.line_count == 3
    assert readme_doc.size_bytes > 0

    src_folder = view.root.folders[1]
    assert [sub.name for sub in src_folder.folders] == ["alpha", "Zeta"]
    assert [doc.file_path for doc in src_folder.files] == [
        "src/app.py",
        "src/Bootstrap.py",
    ]


def test_filesystem_view_filters_tests_prefix_and_max_depth(
    indexed_view_repo: tuple[IndexerEngine, Path, Path],
) -> None:
    """Supports ``include_tests=False``, relative/absolute ``path_prefix``, and ``max_depth``."""
    engine, repo_root, _ = indexed_view_repo

    no_tests_view = engine.views.get_filesystem_view(
        FilesystemViewQuery(include_tests=False)
    )
    assert [sub.name for sub in no_tests_view.root.folders] == ["Docs", "src"]

    for raw_prefix in ("src", "./src/", str(repo_root / "src")):
        focused = engine.views.get_filesystem_view(
            FilesystemViewQuery(path_prefix=raw_prefix, include_tests=False)
        )
        assert focused.root.name == "src"
        assert focused.root.path == "src"
        assert [sub.name for sub in focused.root.folders] == ["alpha", "Zeta"]
        assert [doc.file_path for doc in focused.root.files] == [
            "src/app.py",
            "src/Bootstrap.py",
        ]

    depth_zero = engine.views.get_filesystem_view(FilesystemViewQuery(max_depth=0))
    assert depth_zero.root.folders == ()
    assert depth_zero.root.files == ()

    depth_one = engine.views.get_filesystem_view(
        FilesystemViewQuery(include_tests=False, max_depth=1)
    )
    assert [sub.name for sub in depth_one.root.folders] == ["Docs", "src"]
    assert all(sub.folders == () and sub.files == () for sub in depth_one.root.folders)
    assert [doc.file_path for doc in depth_one.root.files] == [
        "pyproject.toml",
        "README.md",
        "Zebra.txt",
    ]

    with pytest.raises(ValidationError) as exc_info:
        engine.views.get_filesystem_view(FilesystemViewQuery(path_prefix="nonexistent"))
    assert exc_info.value.error_code == "view_path_not_found"
