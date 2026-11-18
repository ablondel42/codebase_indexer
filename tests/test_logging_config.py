"""Logging configuration tests verifying stderr-only console output and safe level checks."""

import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import repo_context.logging_config as logging_config
from repo_context.logging_config import get_logger, setup_logging


@pytest.fixture(autouse=True)
def reset_logging_state() -> Iterator[None]:
    """Reset ``repo_context`` logger handlers and initialization flag around each test."""
    root_logger = logging.getLogger("repo_context")
    old_handlers = list(root_logger.handlers)
    old_level = root_logger.level
    old_flag = logging_config.LOGGING_INITIALIZED
    root_logger.handlers.clear()
    logging_config.LOGGING_INITIALIZED = False
    try:
        yield
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            if isinstance(handler, logging.FileHandler):
                handler.close()
        for handler in old_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(old_level)
        logging_config.LOGGING_INITIALIZED = old_flag


class TestGetLogger:
    """Tests for ``get_logger``."""

    def test_get_logger_namespacing(self) -> None:
        logger = get_logger("storage.nodes")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "repo_context.storage.nodes"
        assert logger is get_logger("storage.nodes")


class TestSetupLogging:
    """Tests for ``setup_logging``."""

    def test_console_handler_uses_stderr_not_stdout(self) -> None:
        """Console logging MUST write to sys.stderr so MCP stdout stays clean."""
        setup_logging(level="INFO")
        root_logger = logging.getLogger("repo_context")
        stream_handlers = [
            h
            for h in root_logger.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        ]
        assert len(stream_handlers) == 1
        assert stream_handlers[0].stream is sys.stderr
        assert stream_handlers[0].stream is not sys.stdout
        assert logging_config.LOGGING_INITIALIZED is True

    def test_setup_logging_skips_if_already_initialized(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        setup_logging(level="INFO")
        with caplog.at_level(logging.WARNING):
            setup_logging(level="DEBUG")
        assert "already initialized" in caplog.text.lower()
        assert logging.getLogger("repo_context").level == logging.INFO

    def test_setup_logging_respects_valid_levels(self) -> None:
        root_logger = logging.getLogger("repo_context")
        setup_logging(level="debug")
        assert root_logger.level == logging.DEBUG

        logging_config.LOGGING_INITIALIZED = False
        setup_logging(level="WARN")
        assert root_logger.level == logging.WARNING

    @pytest.mark.parametrize("bad_level", ["TRACE", "INVALID", "", 123])
    def test_setup_logging_rejects_invalid_levels(self, bad_level: object) -> None:
        with pytest.raises(ValueError):
            setup_logging(level=bad_level)  # type: ignore[arg-type]

    def test_setup_logging_with_file_handler(self, tmp_path: Path) -> None:
        log_file = tmp_path / "app.log"
        setup_logging(level="INFO", log_file=log_file)
        root_logger = logging.getLogger("repo_context")

        assert any(isinstance(h, logging.FileHandler) for h in root_logger.handlers)
        assert log_file.exists()

        get_logger("test_file").info("hello file log")
        for h in root_logger.handlers:
            h.flush()
        assert "hello file log" in log_file.read_text(encoding="utf-8")

    def test_failed_file_handler_preserves_console_level_and_stderr(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """If FileHandler fails to open, console handler stays at requested level on stderr."""
        bad_log_path = tmp_path / "missing_dir" / "app.log"
        setup_logging(level="DEBUG", log_file=bad_log_path)

        root_logger = logging.getLogger("repo_context")
        assert root_logger.level == logging.DEBUG
        stream_handlers = [
            h
            for h in root_logger.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        ]
        assert len(stream_handlers) == 1
        assert stream_handlers[0].stream is sys.stderr

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "failed to create log file handler" in captured.err.lower()
