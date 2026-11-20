"""Tests for path manipulation, ID derivation, and repository security utilities."""

import re
from pathlib import Path

import pytest

from core.models.exceptions import PathSecurityError
from core.utils.pathing import (
    derive_file_id,
    derive_module_path,
    derive_repo_id,
    ensure_safe_repo_file,
    normalize_repo_root,
    to_file_uri,
    to_relative_path,
)


class TestNormalizeRepoRoot:
    """Tests for ``normalize_repo_root``."""

    def test_valid_directory_and_string(self, tmp_path: Path) -> None:
        """Directory paths and string representations resolve to absolute Paths."""
        result = normalize_repo_root(tmp_path)
        assert result.is_absolute()
        assert result == tmp_path.resolve()
        assert normalize_repo_root(str(tmp_path)) == tmp_path.resolve()

    def test_nonexistent_and_file_raise(self, tmp_path: Path) -> None:
        """Missing paths raise FileNotFoundError and files raise NotADirectoryError."""
        with pytest.raises(FileNotFoundError):
            normalize_repo_root(tmp_path / "missing")

        file_path = tmp_path / "file.txt"
        file_path.write_text("content", encoding="utf-8")
        with pytest.raises(NotADirectoryError):
            normalize_repo_root(file_path)


class TestIdDerivation:
    """Tests for collision-resistant repo and file ID derivation."""

    def test_derive_repo_id_format_and_uniqueness(self, tmp_path: Path) -> None:
        """Repo IDs embed the directory name and 8-character resolved-path hash."""
        repo_a = tmp_path / "a" / "service"
        repo_b = tmp_path / "b" / "service"
        repo_a.mkdir(parents=True)
        repo_b.mkdir(parents=True)

        id_a = derive_repo_id(repo_a)
        id_b = derive_repo_id(repo_b)

        assert re.fullmatch(r"project:service:[0-9a-f]{8}", id_a)
        assert re.fullmatch(r"project:service:[0-9a-f]{8}", id_b)
        assert id_a != id_b
        assert id_a == derive_repo_id(repo_a)

    def test_derive_file_id_format(self, tmp_path: Path) -> None:
        """File IDs combine repo_id and POSIX relative path."""
        repo_id = derive_repo_id(tmp_path)
        file_id = derive_file_id(repo_id, "pkg/mod.py")
        assert file_id == f"file:{repo_id}:pkg/mod.py"


class TestRelativePathAndUri:
    """Tests for ``to_relative_path`` and ``to_file_uri``."""

    def test_to_relative_path_posix(self, tmp_path: Path) -> None:
        """Relative paths use POSIX forward slashes."""
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        target = nested / "file.py"
        target.write_text("", encoding="utf-8")

        rel = to_relative_path(tmp_path, target)
        assert rel == "a/b/file.py"
        assert "\\" not in rel

    def test_to_file_uri(self, tmp_path: Path) -> None:
        """Filesystem paths convert to file:// URIs."""
        target = tmp_path / "test.py"
        target.write_text("", encoding="utf-8")
        uri = to_file_uri(target)
        assert uri.startswith("file://")
        assert "test.py" in uri


class TestDeriveModulePath:
    """Tests for ``derive_module_path``."""

    def test_regular_and_nested_packages(self, tmp_path: Path) -> None:
        """Nested modules and __init__.py files derive expected dotted module paths."""
        repo = tmp_path / "my_pkg"
        (repo / "app" / "services").mkdir(parents=True)
        auth = repo / "app" / "services" / "auth.py"
        auth.write_text("", encoding="utf-8")
        init_file = repo / "app" / "__init__.py"
        init_file.write_text("", encoding="utf-8")
        main_file = repo / "main.py"
        main_file.write_text("", encoding="utf-8")
        root_init = repo / "__init__.py"
        root_init.write_text("", encoding="utf-8")

        assert derive_module_path(repo, auth) == "app.services.auth"
        assert derive_module_path(repo, init_file) == "app"
        assert derive_module_path(repo, main_file) == "main"
        assert derive_module_path(repo, root_init) == "my_pkg"


class TestEnsureSafeRepoFile:
    """Security boundary tests for ``ensure_safe_repo_file``."""

    def test_accepts_regular_in_repo_file(self, tmp_path: Path) -> None:
        """Regular non-symlink files inside repo_root resolve cleanly."""
        repo = tmp_path / "repo"
        (repo / "pkg").mkdir(parents=True)
        target = repo / "pkg" / "mod.py"
        target.write_text("x = 1\n", encoding="utf-8")

        assert ensure_safe_repo_file(repo, "pkg/mod.py") == target.resolve()
        assert ensure_safe_repo_file(repo, target) == target.resolve()

    def test_rejects_symlink_pointing_outside_repo_root(self, tmp_path: Path) -> None:
        """Leaf symlinks escaping repo_root raise PathSecurityError."""
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "secret.py"
        outside.write_text("SECRET = 1\n", encoding="utf-8")
        link = repo / "escape.py"
        link.symlink_to(outside)

        with pytest.raises(PathSecurityError):
            ensure_safe_repo_file(repo, link)

    def test_rejects_symlink_pointing_inside_repo(self, tmp_path: Path) -> None:
        """Leaf symlinks pointing inside repo_root also raise PathSecurityError."""
        repo = tmp_path / "repo"
        repo.mkdir()
        real_file = repo / "real.py"
        real_file.write_text("X = 1\n", encoding="utf-8")
        link = repo / "alias.py"
        link.symlink_to(real_file)

        with pytest.raises(PathSecurityError):
            ensure_safe_repo_file(repo, "alias.py")

    def test_rejects_intermediate_symlink_directory(self, tmp_path: Path) -> None:
        """Intermediate symlink directories below repo_root raise PathSecurityError."""
        repo = tmp_path / "repo"
        repo.mkdir()
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "mod.py").write_text("X = 1\n", encoding="utf-8")
        (repo / "linked_pkg").symlink_to(outside_dir)

        with pytest.raises(PathSecurityError):
            ensure_safe_repo_file(repo, repo / "linked_pkg" / "mod.py")

    def test_rejects_broken_symlink(self, tmp_path: Path) -> None:
        """Broken symlinks raise PathSecurityError."""
        repo = tmp_path / "repo"
        repo.mkdir()
        broken = repo / "broken.py"
        broken.symlink_to(tmp_path / "does_not_exist.py")

        with pytest.raises(PathSecurityError):
            ensure_safe_repo_file(repo, broken)

    def test_rejects_traversal_missing_and_directory(self, tmp_path: Path) -> None:
        """Parent traversal, missing files, and directories raise PathSecurityError."""
        repo = tmp_path / "repo"
        (repo / "subdir").mkdir(parents=True)
        outside = tmp_path / "outside.py"
        outside.write_text("X = 1\n", encoding="utf-8")

        with pytest.raises(PathSecurityError):
            ensure_safe_repo_file(repo, "../outside.py")

        with pytest.raises(PathSecurityError):
            ensure_safe_repo_file(repo, "missing.py")

        with pytest.raises(PathSecurityError):
            ensure_safe_repo_file(repo, "subdir")

    def test_rejects_mixed_resolved_root_and_unresolved_ancestor_internal_symlink(
        self,
        tmp_path: Path,
    ) -> None:
        """Symlinks below repo_root are rejected even when ancestor paths use a symlink."""
        real_parent = tmp_path / "real_parent"
        repo = real_parent / "repo"
        (repo / "real_sub").mkdir(parents=True)
        real_file = repo / "real_sub" / "secret.py"
        real_file.write_text("X = 1\n", encoding="utf-8")
        (repo / "link_sub").symlink_to(repo / "real_sub")

        alias_parent = tmp_path / "alias_parent"
        alias_parent.symlink_to(real_parent)

        resolved_root = repo.resolve()
        alias_root = alias_parent / "repo"

        assert (
            ensure_safe_repo_file(resolved_root, alias_root / "real_sub" / "secret.py")
            == real_file.resolve()
        )

        with pytest.raises(PathSecurityError):
            ensure_safe_repo_file(resolved_root, alias_root / "link_sub" / "secret.py")

        with pytest.raises(PathSecurityError):
            ensure_safe_repo_file(
                resolved_root,
                alias_root / "real_sub" / ".." / "link_sub" / "secret.py",
            )
