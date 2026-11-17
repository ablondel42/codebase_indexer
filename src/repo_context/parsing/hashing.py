"""Content hashing utilities for repository scanning and symbol extraction."""

import ast
import hashlib
from collections.abc import Sequence
from pathlib import Path

from repo_context.exceptions import ParseError
from repo_context.logging_config import get_logger

logger = get_logger("parsing.hashing")


def sha256_text(text: str) -> str:
    """Compute SHA-256 hash of a text string.

    Args:
        text: The text to hash.

    Returns:
        Hash string with ``sha256:`` prefix.
    """
    hash_obj = hashlib.sha256(text.encode("utf-8"))
    return f"sha256:{hash_obj.hexdigest()}"


def sha256_file(file_path: Path, max_file_size_bytes: int | None = None) -> str:
    """Compute SHA-256 hash of a file's contents.

    Args:
        file_path: Path to the file to hash.
        max_file_size_bytes: Optional maximum file size in bytes. When provided
            and exceeded, logs a warning and raises ``ParseError``.

    Returns:
        Hash string with ``sha256:`` prefix.

    Raises:
        FileNotFoundError: If the file does not exist.
        PermissionError: If the file cannot be read.
        ParseError: If the file exceeds ``max_file_size_bytes``.
    """
    if max_file_size_bytes is not None:
        size_bytes = file_path.stat().st_size
        if size_bytes > max_file_size_bytes:
            logger.warning(
                "Skipping hashing oversized file %s (%d bytes > %d max_file_size_bytes)",
                file_path,
                size_bytes,
                max_file_size_bytes,
            )
            raise ParseError(
                f"File exceeds max_file_size_bytes ({max_file_size_bytes}): {file_path}",
                context={"file_path": str(file_path), "size_bytes": size_bytes},
            )

    hash_obj = hashlib.sha256()

    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            hash_obj.update(chunk)

    return f"sha256:{hash_obj.hexdigest()}"


def compute_symbol_content_hash(
    node: ast.AST,
    lines: Sequence[str] | None,
    start_line: int,
    end_line: int,
) -> str:
    """Compute SHA-256 content hash for a symbol from its source lines.

    Falls back to ``ast.unparse(node)`` if ``lines`` is unavailable.

    Args:
        node: AST node for the symbol.
        lines: Optional pre-split file lines.
        start_line: 1-based inclusive start line.
        end_line: 1-based inclusive end line.

    Returns:
        Hash string with ``sha256:`` prefix.
    """
    if lines and 1 <= start_line <= end_line <= len(lines):
        snippet = "\n".join(lines[start_line - 1 : end_line])
        return sha256_text(snippet)
    try:
        return sha256_text(ast.unparse(node))
    except Exception:
        return sha256_text("")
