"""Smoke tests for v0.2.0 package imports, configuration, and grouped CLI help."""

import os
from pathlib import Path
import subprocess
import sys

import repo_context
from repo_context.config import AppConfig, get_config
from repo_context.exceptions import RepoContextError
from repo_context.models import (
    Edge,
    FileRecord,
    PlanAssessment,
    Position,
    Range,
    RepoRecord,
    SymbolContext,
    SymbolNode,
    from_json,
    to_json,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"


def _subprocess_env() -> dict[str, str]:
    """Build subprocess environment with ``src/`` prioritized on ``PYTHONPATH``."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing}" if existing else str(SRC_DIR)
    return env


def test_import_package_version() -> None:
    """Package imports cleanly at version 0.2.0."""
    assert repo_context.__version__ == "0.2.0"


def test_import_config() -> None:
    """Config exposes v0.2.0 defaults and supports overrides."""
    config = get_config()
    assert isinstance(config, AppConfig)
    assert config.app_name == "repo-context-mcp"
    assert config.debug is False
    assert config.include_tests is False
    assert config.max_file_size_bytes == 2 * 1024 * 1024
    assert config.lsp_timeout_seconds == 15.0
    assert config.max_lsp_payload_bytes == 16 * 1024 * 1024
    assert ".git" in config.ignored_dirs
    assert "*.egg-info" in config.ignored_dirs
    assert ".nox" in config.ignored_dirs
    assert config.test_dir_names == ("tests", "test")
    assert ".py" in config.supported_extensions

    custom = get_config(db_path="/tmp/custom.db", include_tests=True)
    assert custom.db_path == Path("/tmp/custom.db")
    assert custom.include_tests is True


def test_import_models_and_packages() -> None:
    """Core packages and models import without optional runtime extras."""
    import repo_context.cli
    import repo_context.context
    import repo_context.graph
    import repo_context.indexing
    import repo_context.lsp
    import repo_context.mcp
    import repo_context.parsing
    import repo_context.storage

    assert Position is not None
    assert Range is not None
    assert RepoRecord is not None
    assert FileRecord is not None
    assert SymbolNode is not None
    assert Edge is not None
    assert SymbolContext is not None
    assert PlanAssessment is not None
    assert RepoContextError is not None
    assert callable(to_json)
    assert callable(from_json)
    assert repo_context.cli is not None
    assert repo_context.mcp is not None
    assert repo_context.indexing is not None


def test_cli_module_help_and_version() -> None:
    """CLI exposes grouped v0.2.0 subcommands and reports version 0.2.0."""
    env = _subprocess_env()
    help_res = subprocess.run(
        [sys.executable, "-m", "repo_context.cli.main", "--help"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert help_res.returncode == 0, help_res.stderr
    for cmd in ("index", "db", "files", "outline", "symbols", "source", "refs", "risk", "mcp-call"):
        assert cmd in help_res.stdout

    ver_res = subprocess.run(
        [sys.executable, "-m", "repo_context.cli.main", "--version"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert ver_res.returncode == 0
    assert "0.2.0" in ver_res.stdout
