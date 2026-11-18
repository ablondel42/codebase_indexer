"""Docstring helpers for extracting documentation summaries."""

import ast

# Constants for docstring extraction
MAX_DOCSTRING_LINES = 3
MAX_DOCSTRING_LENGTH = 200
TRUNCATION_SUFFIX = "..."


def _extract_first_paragraph_lines(docstring: str) -> list[str]:
    """Collect stripped non-empty lines up to the first blank line."""
    meaningful_lines: list[str] = []
    for line in docstring.splitlines():
        stripped = line.strip()
        if stripped:
            meaningful_lines.append(stripped)
        elif meaningful_lines:
            break
    return meaningful_lines


def get_doc_summary(node: ast.AST) -> str | None:
    """Extract a short summary from a node's docstring.

    Args:
        node: AST node (Module, ClassDef, FunctionDef, or AsyncFunctionDef).

    Returns:
        First short meaningful paragraph or line, or None if no docstring.
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
