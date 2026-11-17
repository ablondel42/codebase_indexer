"""Naming, signature formatting, and visibility helpers for AST symbols."""

import ast
import re

_WHITESPACE_RE = re.compile(r"\s+")
DEFAULT_MAX_SIGNATURE_LENGTH = 240


class DuplicateTracker:
    """Track symbol names to detect duplicates within a file extraction.

    When duplicate ``(kind, qualified_name)`` pairs are detected, adds a
    disambiguation suffix to the symbol ID.
    """

    def __init__(self) -> None:
        """Initialize empty duplicate tracker."""
        self._seen: dict[tuple[str, str], int] = {}

    def get_symbol_id(self, repo_id: str, kind: str, qualified_name: str) -> str:
        """Get symbol ID, adding disambiguation suffix if duplicate.

        Args:
            repo_id: Repository ID.
            kind: Symbol kind.
            qualified_name: Symbol qualified name.

        Returns:
            Symbol ID with optional ``:dup{N}`` suffix for duplicates.
        """
        key = (kind, qualified_name)
        count = self._seen.get(key, 0)
        self._seen[key] = count + 1

        base_id = f"sym:{repo_id}:{kind}:{qualified_name}"
        if count > 0:
            return f"{base_id}:dup{count}"
        return base_id


def safe_unparse(node: ast.AST) -> str:
    """Safely unparse an AST node to source text."""
    try:
        return ast.unparse(node)
    except Exception:
        return "<unparsable>"


def cap_signature(signature: str, max_length: int = DEFAULT_MAX_SIGNATURE_LENGTH) -> str:
    """Collapse internal whitespace/newlines and cap signature length."""
    normalized = _WHITESPACE_RE.sub(" ", signature).strip()
    if len(normalized) <= max_length:
        return normalized
    if max_length <= 3:
        return normalized[:max_length]
    return normalized[: max_length - 3] + "..."


def compute_visibility_hint(name: str, scope: str) -> str:
    """Compute visibility hint from symbol name and enclosing scope.

    Args:
        name: Simple symbol name.
        scope: Symbol scope (``module``, ``class``, or ``function``).

    Returns:
        ``"local"`` for function-scoped declarations, ``"private_like"`` for
        single-underscore private names, or ``"public"``.
    """
    if scope == "function":
        return "local"
    if name.startswith("_") and not (name.startswith("__") and name.endswith("__")):
        return "private_like"
    return "public"


def build_module_qualified_name(module_path: str) -> str:
    """Build qualified name for a module."""
    return module_path


def build_class_qualified_name(module_path: str, class_name: str) -> str:
    """Build qualified name for a top-level class."""
    if module_path:
        return f"{module_path}.{class_name}"
    return class_name


def build_callable_qualified_name(parent_qualified_name: str, callable_name: str) -> str:
    """Build qualified name for a callable (function or method)."""
    if parent_qualified_name:
        return f"{parent_qualified_name}.{callable_name}"
    return callable_name


def build_nested_qualified_name(module_path: str, lexical_chain: list[str], name: str) -> str:
    """Build qualified name including lexical nesting path."""
    parts = [module_path] if module_path else []
    parts.extend(lexical_chain)
    parts.append(name)
    return ".".join(parts)


def build_module_node_id(repo_id: str, module_path: str) -> str:
    """Build stable node ID for a module."""
    return f"sym:{repo_id}:module:{module_path}"


def build_class_node_id(repo_id: str, qualified_name: str) -> str:
    """Build stable node ID for a class."""
    return f"sym:{repo_id}:class:{qualified_name}"


def build_callable_node_id(repo_id: str, kind: str, qualified_name: str) -> str:
    """Build stable node ID for a callable."""
    return f"sym:{repo_id}:{kind}:{qualified_name}"
