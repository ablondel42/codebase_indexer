"""Immutable application configuration model and builder helper."""

from pathlib import Path
from typing import Any

from pydantic import field_validator

from core.models.base import FrozenModel

IGNORED_DIRS: tuple[str, ...] = (
    # Version control
    ".git",
    ".hg",
    ".svn",
    # Virtual environments
    ".venv",
    "venv",
    "env",
    ".env",
    # Python cache & test runners
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".eggs",
    "*.egg-info",
    ".hypothesis",
    "htmlcov",
    # Build artifacts & package managers
    "build",
    "dist",
    "site",
    "node_modules",
    ".pixi",
    # IDE
    ".idea",
    ".vscode",
    ".cursor",
)


class AppConfig(FrozenModel):
    """Immutable runtime configuration for indexing, storage, and LSP services."""

    app_name: str = "codebase-indexer"
    db_path: Path = Path("codebase_indexer.db")
    debug: bool = False
    include_tests: bool = False
    max_file_size_bytes: int = 2 * 1024 * 1024
    lsp_timeout_seconds: float = 15.0
    max_lsp_payload_bytes: int = 16 * 1024 * 1024
    ignored_dirs: tuple[str, ...] = IGNORED_DIRS
    test_dir_names: tuple[str, ...] = ("tests", "test")
    supported_extensions: tuple[str, ...] = (".py",)

    @field_validator("db_path", mode="before")
    @classmethod
    def _coerce_db_path(cls, value: Any) -> Any:
        """Coerce string database paths into ``Path`` instances.

        Args:
            value: Raw ``db_path`` value passed during validation.

        Returns:
            ``Path`` when ``value`` is a ``str``, otherwise ``value`` unchanged.
        """
        if isinstance(value, str):
            return Path(value)
        return value


def build_config(**overrides: Any) -> AppConfig:
    """Construct an ``AppConfig`` instance with optional keyword overrides.

    Args:
        **overrides: Explicit field overrides for ``AppConfig``.

    Returns:
        Validated immutable ``AppConfig`` instance.

    Raises:
        pydantic.ValidationError: If any override violates ``AppConfig`` schema rules.
    """
    return AppConfig(**overrides)
