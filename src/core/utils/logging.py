"""Application logging utilities rooted at the ``indexer`` logger namespace."""

import logging
import sys
from pathlib import Path
from types import MappingProxyType

ROOT_LOGGER_NAME = "indexer"

_VALID_LOG_LEVELS = MappingProxyType(
    {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
)


def _resolve_log_level(level: str) -> int:
    """Resolve a case-insensitive log level string to its numeric constant.

    Args:
        level: Log level name (``DEBUG``, ``INFO``, ``WARNING``, ``WARN``, ``ERROR``, ``CRITICAL``).

    Returns:
        Numeric ``logging`` level constant.

    Raises:
        ValueError: If ``level`` is not a string or is not a recognized log level.
    """
    if not isinstance(level, str):
        raise ValueError(f"Log level must be a string, got {type(level).__name__}")
    normalized = level.strip().upper()
    if normalized not in _VALID_LOG_LEVELS:
        valid = ", ".join(("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
        raise ValueError(f"Invalid log level {level!r}; expected one of: {valid}")
    return _VALID_LOG_LEVELS[normalized]


def get_logger(name: str) -> logging.Logger:
    """Return a child logger scoped under the ``indexer`` root namespace.

    Args:
        name: Dot-delimited module identifier (e.g., ``"storage.services.nodes"``).
            Passing ``""`` returns the ``"indexer"`` root logger directly.

    Returns:
        Configured or unconfigured ``logging.Logger`` instance.
    """
    if name == "":
        return logging.getLogger(ROOT_LOGGER_NAME)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


def is_logging_configured() -> bool:
    """Return ``True`` when the ``indexer`` root logger already owns handlers.

    Returns:
        ``True`` if at least one handler is attached to ``logging.getLogger("indexer")``.
    """
    return bool(logging.getLogger(ROOT_LOGGER_NAME).handlers)


def configure_logging(
    level: str = "INFO",
    log_file: Path | None = None,
) -> None:
    """Configure ``stderr`` and optional file logging on the ``indexer`` root logger.

    All console log records write strictly to ``sys.stderr`` (with ``propagate = False``)
    so ``sys.stdout`` remains clean for CLI JSON and MCP ``stdio`` framing.

    Args:
        level: Log level name (default ``"INFO"``).
        log_file: Optional file path for persistent UTF-8 log output.

    Raises:
        ValueError: If ``level`` is not a valid log level string.
    """
    root_logger = logging.getLogger(ROOT_LOGGER_NAME)
    if is_logging_configured():
        root_logger.warning("Logging already configured on '%s'; skipping setup", ROOT_LOGGER_NAME)
        return

    numeric_level = _resolve_log_level(level)
    root_logger.setLevel(numeric_level)
    root_logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d (%(funcName)s): %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

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
            root_logger.warning(
                "Failed to create log file handler (%s): %s. Using stderr only.",
                log_file,
                exc,
            )


def reset_logging() -> None:
    """Close and detach all handlers from the ``indexer`` root logger for test isolation."""
    root_logger = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        handler.close()
    root_logger.setLevel(logging.NOTSET)
    root_logger.propagate = False
