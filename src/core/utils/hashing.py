"""SHA-256 content hashing utilities for files, raw text, and AST symbol slices."""

import ast
import hashlib
from collections.abc import Sequence
from pathlib import Path

from core.models.enums import PREFIX_SHA256
from core.models.exceptions import ParseError
from core.utils.logging import get_logger

logger = get_logger("core.utils.hashing")


def sha256_text(text: str) -> str:
    """Compute a ``sha256:``-prefixed digest of a UTF-8 text string.

    Args:
        text: Input string to hash.

    Returns:
        Deterministic digest string prefixed with ``"sha256:"``.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{PREFIX_SHA256}{digest}"


def sha256_file(file_path: Path, max_file_size_bytes: int | None = None) -> str:
    """Compute a ``sha256:``-prefixed digest of a file's raw bytes on disk.

    Args:
        file_path: Path to the file to hash.
        max_file_size_bytes: Optional maximum permitted size in bytes. When
            exceeded, logs a warning and raises ``ParseError``.

    Returns:
        Deterministic digest string prefixed with ``"sha256:"``.

    Raises:
        FileNotFoundError: If ``file_path`` does not exist.
        PermissionError: If ``file_path`` cannot be opened for reading.
        OSError: If stating or reading ``file_path`` fails.
        ParseError: If the file size exceeds ``max_file_size_bytes``.
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
    with open(file_path, "rb") as handle:
        while chunk := handle.read(8192):
            hash_obj.update(chunk)

    return f"{PREFIX_SHA256}{hash_obj.hexdigest()}"


def compute_symbol_content_hash(
    node: ast.AST,
    lines: Sequence[str] | None,
    start_line: int,
    end_line: int,
) -> str:
    """Compute a SHA-256 content hash for a symbol from its 1-based source lines.

    Falls back to ``ast.unparse(node)`` if ``lines`` is unavailable or out of bounds,
    logging a warning if AST unparsing fails.

    Args:
        node: AST node representing the symbol declaration.
        lines: Optional pre-split source lines of the containing file.
        start_line: One-based inclusive start line of the symbol.
        end_line: One-based inclusive end line of the symbol.

    Returns:
        Deterministic digest string prefixed with ``"sha256:"``.
    """
    if lines and 1 <= start_line <= end_line <= len(lines):
        snippet = "\n".join(lines[start_line - 1 : end_line])
        return sha256_text(snippet)

    try:
        return sha256_text(ast.unparse(node))
    except (ValueError, TypeError, RecursionError) as exc:
        logger.warning(
            "Failed to unparse AST node (%s) at lines %d-%d for symbol content hash: %s",
            type(node).__name__,
            start_line,
            end_line,
            exc,
        )
        return sha256_text("")
