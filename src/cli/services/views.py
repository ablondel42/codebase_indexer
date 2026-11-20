"""CLI command handlers for ``cbi view`` structural repository projections."""

import argparse

from cli.utils.output import emit_json, resolve_scoped_engine
from core.utils.logging import get_logger
from engine.services.facade import IndexerEngine
from views.models.filesystem import FilesystemViewQuery

logger = get_logger("cli.services.views")


class ViewCliCommands:
    """Executes ``cbi view`` commands over the headless ``ViewEngine``."""

    def __init__(self, engine: IndexerEngine) -> None:
        """Initialize repository view CLI command handlers."""
        self._engine = engine

    def run_tree(self, args: argparse.Namespace) -> int:
        """Emit the repository folder-and-file Explorer tree as indented JSON."""
        scoped = resolve_scoped_engine(self._engine, args)
        logger.debug("Running cbi view tree (db_path=%s)", scoped.db_path)
        view = scoped.views.get_filesystem_view(
            FilesystemViewQuery(
                repo=getattr(args, "repo", None),
                path_prefix=getattr(args, "path", None),
                include_tests=not bool(getattr(args, "no_tests", False)),
                max_depth=getattr(args, "max_depth", None),
            )
        )
        emit_json(view.model_dump())
        return 0


__all__ = [
    "ViewCliCommands",
]
