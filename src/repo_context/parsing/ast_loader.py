"""AST loader for parsing Python files safely."""

import ast
from pathlib import Path

from repo_context.exceptions import ParseError

DEFAULT_MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024


def load_file_text(
    file_path: Path,
    max_file_size_bytes: int | None = None,
) -> str:
    """Load file content as UTF-8 text while enforcing a size bound.

    Args:
        file_path: Path to the Python file.
        max_file_size_bytes: Optional maximum file size in bytes. Defaults to
            2 MiB when omitted.

    Returns:
        File content as a string.

    Raises:
        FileNotFoundError: If the file does not exist.
        ParseError: If the file exceeds ``max_file_size_bytes`` or cannot be
            decoded as UTF-8.
    """
    limit = max_file_size_bytes if max_file_size_bytes is not None else DEFAULT_MAX_FILE_SIZE_BYTES
    size_bytes = file_path.stat().st_size
    if size_bytes > limit:
        raise ParseError(
            f"File exceeds max_file_size_bytes ({limit}): {file_path}",
            context={"file_path": str(file_path), "size_bytes": size_bytes, "limit": limit},
        )

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
    except UnicodeDecodeError as exc:
        raise ParseError(
            f"Failed to decode UTF-8 file {file_path}: {exc}",
            context={"file_path": str(file_path)},
        ) from exc

    encoded_len = len(text.encode("utf-8"))
    if encoded_len > limit:
        raise ParseError(
            f"File exceeds max_file_size_bytes ({limit}): {file_path}",
            context={"file_path": str(file_path), "size_bytes": encoded_len, "limit": limit},
        )

    return text


def parse_file(text: str, *, filename: str | Path = "<unknown>") -> ast.Module:
    """Parse Python source text into an AST.

    Args:
        text: Python source code as a string.
        filename: Optional filename for diagnostic context.

    Returns:
        Parsed ``ast.Module`` node.

    Raises:
        ParseError: On ``SyntaxError``, ``UnicodeDecodeError``, ``ValueError``,
            or ``RecursionError``.
    """
    try:
        return ast.parse(text, filename=str(filename))
    except (SyntaxError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ParseError(
            f"Failed to parse {filename}: {exc}",
            context={"filename": str(filename), "error_type": type(exc).__name__},
        ) from exc
