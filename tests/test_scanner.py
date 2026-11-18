"""Repository scanner tests covering v0.2.0 IDs, ignore globs, test flags, and security."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

from repo_context.config import AppConfig
from repo_context.exceptions import ParseError, PathSecurityError
from repo_context.parsing.scanner import (
    build_file_record,
    build_repo_record,
    is_supported_source_file,
    is_test_path,
    scan_repository,
    should_ignore_dir,
)
from repo_context.storage import (
    close_connection,
    connection_scope,
    files_table,
    get_connection,
    get_repo_by_id,
    initialize_database,
    list_files_for_repo,
    upsert_files,
    upsert_repo,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing}" if existing else str(SRC_DIR)
    return env


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """Create an isolated sample repository in ``tmp_path``."""
    repo = tmp_path / "sample_repo"
    repo.mkdir()

    (repo / "__init__.py").write_text('"""Root package."""\n', encoding="utf-8")
    (repo / "app").mkdir()
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "services").mkdir()
    (repo / "app" / "services" / "auth.py").write_text(
        "def login():\n    return True\n", encoding="utf-8"
    )
    (repo / "main.py").write_text("from app import services\n", encoding="utf-8")

    # Test files under tests/ and test/
    (repo / "tests").mkdir()
    (repo / "tests" / "test_auth.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (repo / "test").mkdir()
    (repo / "test" / "test_main.py").write_text(
        "def test_main():\n    assert True\n", encoding="utf-8"
    )

    # Ignored directories including fnmatch *.egg-info and .nox
    for ignored in (".git", "__pycache__", "node_modules", ".nox", "pkg_name.egg-info"):
        d = repo / ignored
        d.mkdir()
        (d / "ignored.py").write_text("SHOULD_NOT_INDEX = True\n", encoding="utf-8")

    (repo / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    return repo


def test_build_repo_and_file_record_v2_ids(tmp_path: Path) -> None:
    """Repo and file records use collision-resistant v0.2.0 IDs and metadata."""
    repo_dir = tmp_path / "my_project"
    repo_dir.mkdir()
    root_init = repo_dir / "__init__.py"
    root_init.write_text("VERSION = '1'\nLINE_TWO = 2\n", encoding="utf-8")

    repo = build_repo_record(repo_dir)
    assert re.fullmatch(r"repo:my_project:[0-9a-f]{8}", repo.id)
    assert repo.name == "my_project"
    assert repo.root_path == str(repo_dir.resolve())

    rec = build_file_record(repo.id, repo_dir, root_init)
    assert rec.id == f"file:{repo.id}:__init__.py"
    assert rec.module_path == "my_project"
    assert rec.line_count == 2
    assert rec.is_test is False
    assert rec.content_hash.startswith("sha256:")


def test_should_ignore_dir_supports_fnmatch_globs() -> None:
    """should_ignore_dir matches exact names and fnmatch patterns like *.egg-info."""
    patterns = (".git", ".nox", "*.egg-info")
    assert should_ignore_dir(".git", patterns) is True
    assert should_ignore_dir(".nox", patterns) is True
    assert should_ignore_dir("repo_context.egg-info", patterns) is True
    assert should_ignore_dir("egg-info", patterns) is False
    assert should_ignore_dir("src", patterns) is False


def test_is_test_path_and_supported_extensions() -> None:
    """is_test_path detects files under test directories."""
    assert is_test_path("tests/test_a.py") is True
    assert is_test_path("pkg/test/helper.py") is True
    assert is_test_path("app/services/auth.py") is False
    assert is_supported_source_file(Path("a.py"), (".py",)) is True
    assert is_supported_source_file(Path("a.txt"), (".py",)) is False


def test_scanner_respects_ignored_globs_root_init_and_include_tests(sample_repo: Path) -> None:
    """scan_repository ignores *.egg-info/.nox and gates test files by include_tests."""
    repo, files_no_tests = scan_repository(sample_repo, AppConfig(include_tests=False))
    paths_no_tests = [f.file_path for f in files_no_tests]

    assert paths_no_tests == [
        "__init__.py",
        "app/__init__.py",
        "app/services/auth.py",
        "main.py",
    ]
    by_path = {f.file_path: f for f in files_no_tests}
    assert by_path["__init__.py"].module_path == "sample_repo"
    assert by_path["app/__init__.py"].module_path == "app"
    assert by_path["app/services/auth.py"].line_count == 2
    assert all(not f.is_test for f in files_no_tests)

    _, files_with_tests = scan_repository(sample_repo, AppConfig(include_tests=True))
    paths_with_tests = [f.file_path for f in files_with_tests]
    assert "tests/test_auth.py" in paths_with_tests
    assert "test/test_main.py" in paths_with_tests
    test_flags = {f.file_path: f.is_test for f in files_with_tests}
    assert test_flags["tests/test_auth.py"] is True
    assert test_flags["test/test_main.py"] is True
    assert test_flags["main.py"] is False
    assert not any("egg-info" in p or ".nox" in p for p in paths_with_tests)


def test_scanner_rejects_symlinks_outside_root_and_survives_broken_symlinks(tmp_path: Path) -> None:
    """scan_repository rejects external symlinks and skips broken symlinks cleanly."""
    repo = tmp_path / "sec_repo"
    repo.mkdir()
    (repo / "good.py").write_text("OK = 1\n", encoding="utf-8")

    outside_file = tmp_path / "outside.py"
    outside_file.write_text("SECRET = 1\n", encoding="utf-8")
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "leak.py").write_text("LEAK = 1\n", encoding="utf-8")

    (repo / "ext_link.py").symlink_to(outside_file)
    (repo / "ext_dir_link").symlink_to(outside_dir)
    (repo / "broken_link.py").symlink_to(tmp_path / "missing_target.py")

    _repo_rec, files = scan_repository(repo, AppConfig())
    assert [f.file_path for f in files] == ["good.py"]

    with pytest.raises(PathSecurityError):
        build_file_record(_repo_rec.id, repo, repo / "ext_link.py")


def test_scanner_enforces_max_file_size_bytes(tmp_path: Path) -> None:
    """Oversized .py files are skipped by scan_repository and rejected by build_file_record."""
    repo = tmp_path / "size_repo"
    repo.mkdir()
    small = repo / "small.py"
    small.write_text("x = 1\n", encoding="utf-8")
    large = repo / "large.py"
    large.write_text("x = 'a'\n" * 100, encoding="utf-8")

    cfg = AppConfig(max_file_size_bytes=100)
    repo_rec, files = scan_repository(repo, cfg)
    assert [f.file_path for f in files] == ["small.py"]

    with pytest.raises(ParseError, match="max_file_size_bytes"):
        build_file_record(repo_rec.id, repo, large, max_file_size_bytes=100)


def test_scan_persists_records_with_include_tests_filter(sample_repo: Path, tmp_path: Path) -> None:
    """Persisted FileRecords support is_test filtering in list_files_for_repo."""
    db_path = tmp_path / "test.db"
    conn = get_connection(db_path)
    try:
        initialize_database(conn)
        repo, file_records = scan_repository(sample_repo, AppConfig(include_tests=True))
        upsert_repo(conn, repo)
        upsert_files(conn, file_records)
        conn.commit()

        assert get_repo_by_id(conn, repo.id) is not None
        assert len(list_files_for_repo(conn, repo.id, include_tests=True)) == 6
        assert len(list_files_for_repo(conn, repo.id, include_tests=False)) == 4
    finally:
        close_connection(conn)


def test_invalid_repo_path_raises(tmp_path: Path) -> None:
    """Nonexistent or non-directory repo roots raise clear errors."""
    cfg = AppConfig()
    with pytest.raises(FileNotFoundError):
        scan_repository(tmp_path / "missing", cfg)

    f = tmp_path / "not_dir.txt"
    f.write_text("hi", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        scan_repository(f, cfg)


def test_rc_index_cli_command(sample_repo: Path, tmp_path: Path) -> None:
    """`rc index` scans and indexes sample_repo into a temporary DB."""
    db_path = tmp_path / "cli_index.db"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "repo_context.cli.main",
            "index",
            str(sample_repo),
            "--db-path",
            str(db_path),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["files_indexed"] == 4
    assert db_path.exists()

    with connection_scope(db_path, read_only=True) as conn:
        count = int(conn.execute(select(func.count()).select_from(files_table)).scalar_one())
        assert count == 4
