"""Top-level ``CliApplication`` wiring ``IndexerEngine``, parsers, handlers, and REPL."""

import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from cli.services.analysis import AnalysisCliCommands
from cli.services.db import DbCliCommands
from cli.services.index import IndexCliCommands
from cli.services.parser import CliParserBuilder
from cli.services.server import ServerCliCommands
from cli.services.shell import InteractiveShell
from cli.services.symbols import SymbolCliCommands
from cli.utils.argv import preprocess_argv
from cli.utils.output import (
    emit_cli_error,
    resolve_cli_db_path,
    silence_broken_pipe,
)
from core.models.config import AppConfig
from core.models.exceptions import (
    DatabaseNotFoundError,
    IndexerError,
    ValidationError,
)
from core.utils.logging import (
    ROOT_LOGGER_NAME,
    configure_logging,
    get_logger,
    is_logging_configured,
)
from engine.services.facade import IndexerEngine

logger = get_logger("cli.services.app")


class CliApplication:
    """Wires CLI subcommand handlers, structured JSON error dispatch, and engine lifecycle."""

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        engine: IndexerEngine | None = None,
    ) -> None:
        """Initialize the CLI application and command handler services.

        Args:
            config: Optional ``AppConfig`` override (defaults to ``AppConfig()``).
            engine: Optional injected ``IndexerEngine`` (if omitted, created and closed by app).
        """
        resolved_config = config if config is not None else AppConfig()
        self._config = resolved_config
        self._owns_engine = engine is None
        self._engine = engine if engine is not None else IndexerEngine(resolved_config)

        self._index_cmds = IndexCliCommands(self._engine)
        self._db_cmds = DbCliCommands(self._engine)
        self._symbol_cmds = SymbolCliCommands(self._engine)
        self._analysis_cmds = AnalysisCliCommands(self._engine)
        self._server_cmds = ServerCliCommands(self._engine)

        self._handlers = self._build_handler_map()
        self._parser_builder = CliParserBuilder(self._handlers)
        self._shell = InteractiveShell(self._engine, self._parser_builder, self.dispatch)

    def _run_shell_cmd(self, args: argparse.Namespace) -> int:
        """Delegate ``rc shell`` execution to ``self._shell``.

        Args:
            args: Parsed ``rc shell`` CLI arguments.

        Returns:
            Exit code returned by ``InteractiveShell.run_shell_command``.
        """
        return self._shell.run_shell_command(args)

    def _build_handler_map(self) -> dict[str, Callable[[argparse.Namespace], int]]:
        """Assemble the subcommand key-to-handler dispatch table.

        Returns:
            Dictionary mapping canonical subcommand keys to bound handler methods.
        """
        return {
            "index": self._index_cmds.run_index,
            "db.status": self._db_cmds.run_status,
            "db.repos": self._db_cmds.run_repos,
            "db.tables": self._db_cmds.run_tables,
            "db.runs": self._db_cmds.run_runs,
            "db.sql": self._db_cmds.run_sql,
            "files": self._symbol_cmds.run_files,
            "outline": self._symbol_cmds.run_outline,
            "symbols.search": self._symbol_cmds.run_search,
            "symbols.list": self._symbol_cmds.run_list,
            "symbols.show": self._symbol_cmds.run_show,
            "source": self._symbol_cmds.run_source,
            "refs": self._analysis_cmds.run_refs,
            "risk": self._analysis_cmds.run_risk,
            "shell": self._run_shell_cmd,
            "watch": self._index_cmds.run_watch,
            "serve": self._server_cmds.run_serve,
            "mcp-call": self._server_cmds.run_mcp_call,
        }

    @property
    def parser_builder(self) -> CliParserBuilder:
        """Return the ``CliParserBuilder`` bound to this application's handlers."""
        return self._parser_builder

    @property
    def shell(self) -> InteractiveShell:
        """Return the ``InteractiveShell`` service bound to this application."""
        return self._shell

    @staticmethod
    def _configure_runtime_logging(args: argparse.Namespace) -> None:
        """Configure ``DEBUG`` stderr logging or attach a silent ``NullHandler``.

        Args:
            args: Parsed root CLI ``argparse.Namespace``.
        """
        is_debug = bool(getattr(args, "debug", False) or getattr(args, "verbose", False))
        root_logger = logging.getLogger(ROOT_LOGGER_NAME)
        if is_debug:
            if root_logger.handlers and all(
                isinstance(h, logging.NullHandler) for h in root_logger.handlers
            ):
                for h in list(root_logger.handlers):
                    root_logger.removeHandler(h)
            if not is_logging_configured():
                configure_logging(level="DEBUG")
            return

        if not root_logger.handlers:
            root_logger.addHandler(logging.NullHandler())

    def _invoke_handler(self, args: argparse.Namespace) -> int:
        """Resolve and call the command handler bound to ``args``.

        Args:
            args: Parsed CLI ``argparse.Namespace``.

        Returns:
            Integer exit code returned by the command handler.

        Raises:
            ValidationError: If no callable handler is bound on ``args``.
        """
        func: Callable[[argparse.Namespace], int] | None = getattr(args, "func", None)
        if callable(func):
            return int(func(args))
        key = getattr(args, "handler_key", None)
        handler = self._handlers.get(str(key)) if key is not None else None
        if handler is None:
            raise ValidationError(f"Unknown CLI command handler: {key!r}")
        return int(handler(args))

    def _execute_guarded(self, action: Callable[[], int]) -> int:
        """Execute ``action`` inside the CLI structured JSON error boundary.

        Args:
            action: Zero-argument callable returning a process exit code.

        Returns:
            Exit code (``0`` on success/broken pipe, ``130`` on Ctrl-C, ``1`` on error).
        """
        exit_code = 1
        try:
            return action()
        except DatabaseNotFoundError as exc:
            logger.debug("Database not found during CLI command: %s", exc)
            emit_cli_error(
                exc.error_code,
                f"{exc}. Run `rc index` first.",
                details=exc.context,
            )
        except IndexerError as exc:
            logger.debug("CLI domain error (%s): %s", exc.error_code, exc.message)
            emit_cli_error(exc.error_code, exc.message, details=exc.context)
        except (
            FileNotFoundError,
            NotADirectoryError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            logger.debug("CLI input or filesystem error (%s): %s", type(exc).__name__, exc)
            emit_cli_error("error", str(exc))
        except BrokenPipeError as exc:
            logger.debug("CLI stdout BrokenPipeError: %s", exc)
            silence_broken_pipe()
            exit_code = 0
        except KeyboardInterrupt as exc:
            logger.debug("CLI command interrupted by user: %s", exc)
            emit_cli_error("interrupted", "Interrupted.")
            exit_code = 130
        except Exception as exc:
            logger.exception("CLI command failed")
            emit_cli_error("internal_error", str(exc))
        return exit_code

    def dispatch(self, args: argparse.Namespace) -> int:
        """Dispatch parsed CLI ``args`` with structured JSON error handling.

        Args:
            args: Parsed CLI ``argparse.Namespace``.

        Returns:
            Process exit code.
        """
        return self._execute_guarded(lambda: self._invoke_handler(args))

    def run(self, argv: Sequence[str] | None = None) -> int:
        """Parse ``argv``, execute the requested subcommand, and close engine resources.

        Args:
            argv: Optional argument list (defaults to ``sys.argv[1:]``).

        Returns:
            Process exit code.
        """
        try:
            parser = self._parser_builder.build()
            raw_argv = list(sys.argv[1:] if argv is None else argv)
            args = parser.parse_args(preprocess_argv(raw_argv))
            self._configure_runtime_logging(args)

            rc = self.dispatch(args)
            if rc == 0 and bool(getattr(args, "shell", False)):
                repo_arg = getattr(args, "repo_path", None)
                resolved_repo = (
                    str(Path(repo_arg).resolve()) if repo_arg else str(Path.cwd().resolve())
                )
                db_path = resolve_cli_db_path(args, self._engine.db_path)
                return self._execute_guarded(
                    lambda: self._shell.run_session(db_path, resolved_repo)
                )
            return rc
        finally:
            if self._owns_engine:
                self._engine.close()


__all__ = [
    "CliApplication",
]
