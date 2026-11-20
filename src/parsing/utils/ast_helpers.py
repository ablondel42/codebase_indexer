"""Pure Python AST loading, parsing, docstring, signature, and module-resolution helpers."""

import ast
import re
from pathlib import Path

from core.models.enums import SymbolScope
from core.models.exceptions import ParseError
from core.models.file import FileRecord
from core.utils.logging import get_logger

logger = get_logger("parsing.utils.ast_helpers")

DEFAULT_MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_SIGNATURE_LENGTH = 240
MAX_DOCSTRING_LINES = 3
MAX_DOCSTRING_LENGTH = 200
TRUNCATION_SUFFIX = "..."

_WHITESPACE_RE = re.compile(r"\s+")
_NESTED_SCOPE_BOUNDARY_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
)


def load_python_source(
    file_path: Path,
    max_file_size_bytes: int | None = None,
) -> str:
    """Load UTF-8 Python source text from ``file_path`` while enforcing a size bound.

    Args:
        file_path: Path to the Python source file on disk.
        max_file_size_bytes: Optional maximum permitted size in bytes (defaults to 2 MiB).

    Returns:
        Decoded UTF-8 file contents as a string.

    Raises:
        FileNotFoundError: If ``file_path`` does not exist on disk.
        OSError: If stating or reading ``file_path`` fails.
        ParseError: If the file exceeds ``max_file_size_bytes`` or fails UTF-8 decoding.
    """
    limit = max_file_size_bytes if max_file_size_bytes is not None else DEFAULT_MAX_FILE_SIZE_BYTES
    size_bytes = file_path.stat().st_size
    if size_bytes > limit:
        logger.warning(
            "Rejecting oversized Python source file %s (%d bytes > %d limit)",
            file_path,
            size_bytes,
            limit,
        )
        raise ParseError(
            f"File exceeds max_file_size_bytes ({limit}): {file_path}",
            context={"file_path": str(file_path), "size_bytes": size_bytes, "limit": limit},
        )

    try:
        with open(file_path, encoding="utf-8") as handle:
            text = handle.read()
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


def parse_python_ast(text: str, *, filename: str | Path = "<unknown>") -> ast.Module:
    """Parse Python source text into an ``ast.Module``.

    Args:
        text: Raw Python source code string.
        filename: Diagnostic filename attached to syntax errors.

    Returns:
        Parsed ``ast.Module`` root node.

    Raises:
        ParseError: If ``ast.parse`` raises ``SyntaxError``, ``UnicodeDecodeError``,
            ``ValueError``, or ``RecursionError``.
    """
    try:
        return ast.parse(text, filename=str(filename))
    except (SyntaxError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ParseError(
            f"Failed to parse {filename}: {exc}",
            context={"filename": str(filename), "error_type": type(exc).__name__},
        ) from exc


def _extract_first_paragraph_lines(docstring: str) -> list[str]:
    """Collect stripped non-empty lines from ``docstring`` up to the first blank line.

    Args:
        docstring: Stripped raw docstring text.

    Returns:
        List of non-empty lines forming the leading paragraph.
    """
    meaningful_lines: list[str] = []
    for line in docstring.splitlines():
        stripped = line.strip()
        if stripped:
            meaningful_lines.append(stripped)
        elif meaningful_lines:
            break
    return meaningful_lines


def get_doc_summary(node: ast.AST) -> str | None:
    """Extract a concise single-paragraph summary from an AST node's docstring.

    Args:
        node: Candidate AST node (``Module``, ``ClassDef``, ``FunctionDef``, or
            ``AsyncFunctionDef``).

    Returns:
        Summary string capped at 200 characters, or ``None`` if no docstring exists.
    """
    if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef, ast.ClassDef, ast.Module)):
        return None

    docstring = ast.get_docstring(node)
    if not docstring or not docstring.strip():
        return None

    meaningful_lines = _extract_first_paragraph_lines(docstring.strip())
    if not meaningful_lines:
        return None

    summary = " ".join(meaningful_lines[:MAX_DOCSTRING_LINES])
    if len(summary) > MAX_DOCSTRING_LENGTH:
        return summary[: MAX_DOCSTRING_LENGTH - len(TRUNCATION_SUFFIX)] + TRUNCATION_SUFFIX
    return summary


def safe_unparse(node: ast.AST) -> str:
    """Safely unparse an AST node to source text, logging ``DEBUG`` on fallback.

    Args:
        node: AST node to render.

    Returns:
        Rendered Python expression/statement text, or ``"<unparsable>"`` on failure.
    """
    try:
        return ast.unparse(node)
    except (ValueError, TypeError, RecursionError, AttributeError) as exc:
        logger.debug(
            "Fallback to '<unparsable>' while unparsing AST node %s: %s",
            type(node).__name__,
            exc,
        )
        return "<unparsable>"


def cap_signature(signature: str, max_length: int = DEFAULT_MAX_SIGNATURE_LENGTH) -> str:
    """Normalize internal whitespace and cap signature length with ``...``.

    Args:
        signature: Raw signature text.
        max_length: Maximum character length (default ``240``).

    Returns:
        Single-line normalized signature capped at ``max_length`` characters.
    """
    normalized = _WHITESPACE_RE.sub(" ", signature).strip()
    if len(normalized) <= max_length:
        return normalized
    if max_length <= len(TRUNCATION_SUFFIX):
        return normalized[:max_length]
    return normalized[: max_length - len(TRUNCATION_SUFFIX)] + TRUNCATION_SUFFIX


def compute_visibility_hint(name: str, scope: SymbolScope | str) -> str:
    """Compute visibility hint from a symbol's simple name and enclosing scope.

    Args:
        name: Simple declared identifier name.
        scope: Enclosing ``SymbolScope`` (``module``, ``class``, or ``function``).

    Returns:
        ``"local"`` for function-scoped declarations, ``"private_like"`` for leading
        underscore names (excluding dunder names), or ``"public"``.
    """
    if scope == SymbolScope.FUNCTION:
        return "local"
    if name.startswith("_") and not (name.startswith("__") and name.endswith("__")):
        return "private_like"
    return "public"


def is_generator_callable(node: ast.AST) -> bool:
    """Check whether a callable body directly contains ``yield`` or ``yield from``.

    Does not descend into nested functions, classes, or lambdas.

    Args:
        node: ``ast.FunctionDef`` or ``ast.AsyncFunctionDef`` node.

    Returns:
        ``True`` if the callable body itself yields, otherwise ``False``.
    """
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.Yield, ast.YieldFrom)):
            return True
        if isinstance(child, _NESTED_SCOPE_BOUNDARY_NODES):
            continue
        stack.extend(ast.iter_child_nodes(child))
    return False


def extract_dotted_parts(expr: ast.AST) -> list[str] | None:
    """Extract simple dotted identifier parts from ``a.b.c`` AST expressions.

    Args:
        expr: Candidate ``ast.Name`` or ``ast.Attribute`` expression.

    Returns:
        Ordered list of identifier parts, or ``None`` if ``expr`` is not a pure chain.
    """
    if isinstance(expr, ast.Name):
        return [expr.id]
    if isinstance(expr, ast.Attribute):
        prefix = extract_dotted_parts(expr.value)
        if prefix is not None:
            return [*prefix, expr.attr]
    return None


def resolve_relative_module(file_record: FileRecord, raw_module: str, level: int) -> str:
    """Resolve a relative ``from ...`` import against ``file_record.module_path``.

    Args:
        file_record: Tracked ``FileRecord`` of the importing module.
        raw_module: Raw ``node.module`` string (``""`` for ``from . import x``).
        level: Leading dot count (``0`` for absolute imports).

    Returns:
        Resolved dotted module path, or ``raw_module`` when ``level`` exceeds package depth.
    """
    if level <= 0:
        return raw_module

    module_path = file_record.module_path or ""
    parts = [part for part in module_path.split(".") if part]
    is_package_init = file_record.file_path == "__init__.py" or file_record.file_path.endswith(
        "/__init__.py"
    )
    package_parts = parts if is_package_init else parts[:-1]

    up_steps = level - 1
    if up_steps > len(package_parts):
        return raw_module

    base_parts = package_parts[: len(package_parts) - up_steps]
    resolved_parts = (
        [*base_parts, *[part for part in raw_module.split(".") if part]]
        if raw_module
        else base_parts
    )
    return ".".join(resolved_parts)


__all__ = [
    "DEFAULT_MAX_FILE_SIZE_BYTES",
    "DEFAULT_MAX_SIGNATURE_LENGTH",
    "MAX_DOCSTRING_LENGTH",
    "MAX_DOCSTRING_LINES",
    "cap_signature",
    "compute_visibility_hint",
    "extract_dotted_parts",
    "get_doc_summary",
    "is_generator_callable",
    "load_python_source",
    "parse_python_ast",
    "resolve_relative_module",
    "safe_unparse",
]
