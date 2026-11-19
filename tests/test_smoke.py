"""Smoke tests for v0.2.0 domain packages, ``AppConfig``, models, and ``cli.main``."""

import importlib
import os
import subprocess
import sys
from pathlib import Path

from context.models.symbol_context import SymbolContext
from core import (
    APP_VERSION,
    AppConfig,
    Edge,
    FileRecord,
    IndexerError,
    Position,
    Range,
    RepoRecord,
    SymbolNode,
    build_config,
)
from core.utils.json_codec import from_json, to_json
from engine import IndexerEngine
from graph.models.risk import RiskAssessment

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"

DOMAIN_PACKAGES: tuple[str, ...] = (
    "core",
    "storage",
    "lsp",
    "parsing",
    "graph",
    "context",
    "indexing",
    "engine",
    "mcp_server",
    "cli",
)


def _subprocess_env() -> dict[str, str]:
    """Build subprocess environment with ``src/`` prioritized on ``PYTHONPATH``."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing}" if existing else str(SRC_DIR)
    return env


def test_import_package_version() -> None:
    """Core version constant reports ``0.2.0`` cleanly."""
    assert APP_VERSION == "0.2.0"


def test_import_config() -> None:
    """``AppConfig`` and ``build_config`` expose v0.2.0 defaults and Path coercion."""
    config = build_config()
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

    custom = build_config(db_path="/tmp/custom.db", include_tests=True)
    assert custom.db_path == Path("/tmp/custom.db")
    assert custom.include_tests is True


def test_import_models_and_packages() -> None:
    """All 10 domain packages, Pydantic entities, and JSON codecs import cleanly."""
    for pkg_name in DOMAIN_PACKAGES:
        module = importlib.import_module(pkg_name)
        assert module is not None

    assert Position is not None
    assert Range is not None
    assert RepoRecord is not None
    assert FileRecord is not None
    assert SymbolNode is not None
    assert Edge is not None
    assert SymbolContext is not None
    assert RiskAssessment is not None
    assert IndexerError is not None
    assert IndexerEngine is not None
    assert callable(to_json)
    assert callable(from_json)


def test_cli_module_help_and_version() -> None:
    """``python -m cli.main`` exposes grouped v0.2.0 subcommands and reports ``0.2.0``."""
    env = _subprocess_env()
    help_res = subprocess.run(
        [sys.executable, "-m", "cli.main", "--help"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert help_res.returncode == 0, help_res.stderr
    expected_commands = (
        "index",
        "db",
        "files",
        "outline",
        "symbols",
        "source",
        "refs",
        "risk",
        "mcp-call",
    )
    for cmd in expected_commands:
        assert cmd in help_res.stdout

    ver_res = subprocess.run(
        [sys.executable, "-m", "cli.main", "--version"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert ver_res.returncode == 0
    assert "0.2.0" in ver_res.stdout
