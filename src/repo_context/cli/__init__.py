"""CLI entry points."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse


def main(argv: Sequence[str] | None = None) -> int:
    """Delegate to ``repo_context.cli.main.main`` without eager package-level import."""
    from repo_context.cli.main import main as _main

    return _main(argv)


def build_parser() -> argparse.ArgumentParser:
    """Delegate to ``repo_context.cli.main.build_parser`` without eager package-level import."""
    from repo_context.cli.main import build_parser as _build_parser

    return _build_parser()


__all__ = ["main", "build_parser"]
