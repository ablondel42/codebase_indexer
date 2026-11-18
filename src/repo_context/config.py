"""Application configuration."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AppConfig:
    """Application configuration."""

    app_name: str = "repo-context-mcp"
    db_path: Path = Path("repo_context.db")
    debug: bool = False
    include_tests: bool = False
    max_file_size_bytes: int = 2 * 1024 * 1024
    lsp_timeout_seconds: float = 15.0
    max_lsp_payload_bytes: int = 16 * 1024 * 1024
    ignored_dirs: tuple[str, ...] = (
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
        # Build artifacts
        "build",
        "dist",
        "node_modules",
        # IDE
        ".idea",
        ".vscode",
        ".agents",
        ".agent",
    )
    test_dir_names: tuple[str, ...] = ("tests", "test")
    supported_extensions: tuple[str, ...] = (".py",)

    def __post_init__(self) -> None:
        if isinstance(self.db_path, str):
            object.__setattr__(self, "db_path", Path(self.db_path))


def get_config(**overrides: Any) -> AppConfig:
    """Get the application configuration with optional field overrides."""
    if "db_path" in overrides and overrides["db_path"] is not None and not isinstance(
        overrides["db_path"], Path
    ):
        overrides["db_path"] = Path(overrides["db_path"])
    return AppConfig(**overrides)
