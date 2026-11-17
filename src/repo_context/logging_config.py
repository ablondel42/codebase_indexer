"""Logging configuration for the application."""

import logging
import sys
from pathlib import Path
from typing import Optional

# Flag to track if logging has been initialized
LOGGING_INITIALIZED: bool = False

_VALID_LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _resolve_log_level(level: str) -> int:
    """Safely resolve a log level name to its numeric logging constant."""
    if not isinstance(level, str):
        raise ValueError(f"Log level must be a string, got {type(level).__name__}")
    normalized = level.strip().upper()
    if normalized not in _VALID_LOG_LEVELS:
        valid = ", ".join(("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
        raise ValueError(f"Invalid log level {level!r}; expected one of: {valid}")
    return _VALID_LOG_LEVELS[normalized]


def setup_logging(
    level: str = "INFO",
    log_file: Optional[Path] = None,
) -> None:
    """Configure logging for the application.

    All console logs are sent to sys.stderr (never sys.stdout, preserving MCP STDIO).

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL). Default is "INFO".
        log_file: Optional file path for log output. If None, only stderr is used.
    """
    global LOGGING_INITIALIZED

    if LOGGING_INITIALIZED:
        logging.getLogger("repo_context").warning(
            "Logging already initialized, skipping setup"
        )
        return

    numeric_level = _resolve_log_level(level)

    root_logger = logging.getLogger("repo_context")
    root_logger.handlers.clear()
    root_logger.setLevel(numeric_level)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d (%(funcName)s): %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Always write console logs to stderr so stdout stays clean for MCP STDIO/JSON CLI.
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    if log_file is not None:
        try:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        except OSError as exc:
            # Keep console handler level intact; warn on stderr and continue.
            root_logger.warning(
                "Failed to create log file handler (%s): %s. Using stderr only.",
                log_file,
                exc,
            )

    LOGGING_INITIALIZED = True


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for the given module.

    Args:
        name: Module name (e.g., "storage.nodes", "cli.main").
              Will be prefixed with "repo_context.".
    """
    return logging.getLogger(f"repo_context.{name}")
