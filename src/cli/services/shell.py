"""Interactive ``rc>`` REPL session service."""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from cli.models.shell import ShellSessionState
from cli.services.parser import CliParserBuilder
from cli.utils.argv import inject_shell_defaults, tokenize_shell_line
from cli.utils.output import emit_cli_error, resolve_cli_db_path
from core.utils.logging import get_logger
from engine.services.facade import IndexerEngine

logger = get_logger("cli.services.shell")


class InteractiveShell:
    """Runs the interactive ``rc>`` command loop over an indexed SQLite database."""

    def __init__(
        self,
        engine: IndexerEngine,
        parser_builder: CliParserBuilder,
        dispatcher: Callable[[argparse.Namespace], int],
    ) -> None:
        """Initialize the interactive shell service.

        Args:
            engine: Headless ``IndexerEngine`` application facade.
            parser_builder: ``CliParserBuilder`` constructing session command parsers.
            dispatcher: Callback executing parsed ``argparse.Namespace`` commands.
        """
        self._engine = engine
        self._parser_builder = parser_builder
        self._dispatcher = dispatcher

    @staticmethod
    def _read_prompt_line() -> tuple[bool, str | None]:
        """Read one prompt line from ``sys.stdin``, returning ``(should_exit, line)``.

        Returns:
            Tuple ``(should_exit, raw_line)`` handling EOF, broken pipe, and Ctrl-C.
        """
        try:
            if sys.stdin.isatty():
                return False, input("rc> ")
            sys.stdout.write("rc> ")
            sys.stdout.flush()
            raw = sys.stdin.readline()
            if raw == "":
                return True, None
            return False, raw.rstrip("\r\n")
        except (EOFError, BrokenPipeError) as exc:
            logger.debug("Interactive shell input closed (%s): %s", type(exc).__name__, exc)
            return True, None
        except KeyboardInterrupt as exc:
            logger.debug("Interactive shell line cancelled by KeyboardInterrupt: %s", exc)
            print("", file=sys.stderr)
            return False, None

    @staticmethod
    def _prepare_tokens(
        stripped: str,
        parser: argparse.ArgumentParser,
        state: ShellSessionState,
    ) -> tuple[bool, list[str] | None]:
        """Tokenize a shell line and handle built-in commands.

        Args:
            stripped: Trimmed non-comment shell input line.
            parser: Root ``ArgumentParser`` used for ``help`` output.
            state: Current ``ShellSessionState`` carrying ``db_path`` and ``active_repo_id``.

        Returns:
            Tuple ``(should_exit, normalized_tokens_or_none)``.
        """
        try:
            tokens = tokenize_shell_line(stripped)
        except ValueError as exc:
            logger.debug("Interactive shell shlex parse error: %s", exc)
            emit_cli_error("parse_error", str(exc))
            return False, None

        if not tokens:
            return False, None
        if tokens[0] in ("exit", "quit", "q"):
            return True, None
        if tokens[0] in ("help", "?", "shell"):
            if tokens[0] == "shell":
                emit_cli_error("invalid_command", "Already inside rc shell.")
            else:
                parser.print_help()
            return False, None

        return False, inject_shell_defaults(tokens, state.db_path, state.active_repo_id)

    def run_session(self, db_path: Path, repo_arg: str | None = None) -> int:
        """Start an interactive ``rc>`` REPL bound to ``db_path``.

        Args:
            db_path: Target SQLite database path (must already exist).
            repo_arg: Optional explicit repository selector string.

        Returns:
            Exit code ``0`` when the user exits the shell.

        Raises:
            DatabaseNotFoundError: If ``db_path`` does not exist on disk.
            DatabaseError: If ``repo_arg`` cannot be resolved.
        """
        active_repo = self._engine.for_db(db_path).queries.resolve_active_repo(repo_arg)
        state = ShellSessionState(db_path=db_path, active_repo_id=active_repo)
        print(
            f"Interactive rc shell (db: {state.db_path}, repo: {state.active_repo_id or 'auto'}). "
            "Type 'help' or 'exit'."
        )
        parser = self._parser_builder.build()

        while True:
            should_exit, line = self._read_prompt_line()
            if should_exit:
                break
            stripped = line.strip() if line is not None else ""
            if not stripped or stripped.startswith("#"):
                continue

            should_exit, tokens = self._prepare_tokens(stripped, parser, state)
            if should_exit:
                break
            if tokens is None:
                continue

            try:
                parsed = parser.parse_args(tokens)
            except SystemExit as exc:
                logger.debug("Shell command exited argument parser (code=%s)", exc.code)
                continue
            self._dispatcher(parsed)

        return 0

    def run_shell_command(self, args: argparse.Namespace) -> int:
        """Execute ``rc shell`` from parsed CLI arguments.

        Args:
            args: Parsed ``rc shell`` CLI arguments.

        Returns:
            Exit code ``0`` when the session ends.
        """
        db_path = resolve_cli_db_path(args, self._engine.db_path)
        return self.run_session(db_path, getattr(args, "repo", None))


__all__ = [
    "InteractiveShell",
]
