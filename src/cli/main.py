"""Thin executable entry point for the grouped ``cbi`` CLI."""

import sys
from collections.abc import Sequence

from cli.services.app import CliApplication


def main(argv: Sequence[str] | None = None) -> int:
    """Execute the ``cbi`` CLI application and return its exit status code.

    Args:
        argv: Optional command-line argument sequence (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code integer.
    """
    return CliApplication().run(argv)


if __name__ == "__main__":
    sys.exit(main())
