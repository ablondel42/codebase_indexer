"""Pure qualified-name and deterministic symbol/edge identifier builders."""

from collections.abc import Sequence

from core.models.enums import PREFIX_EDGE, PREFIX_SYMBOL, EdgeKind, SymbolKind


def build_module_qualified_name(module_path: str) -> str:
    """Build the canonical qualified name for a Python module.

    Args:
        module_path: Dotted Python module path.

    Returns:
        Module qualified name string.
    """
    return module_path


def build_class_qualified_name(module_path: str, class_name: str) -> str:
    """Build the canonical qualified name for a top-level class.

    Args:
        module_path: Dotted Python module path.
        class_name: Simple declared class name.

    Returns:
        Dotted qualified name for the class.
    """
    if module_path:
        return f"{module_path}.{class_name}"
    return class_name


def build_callable_qualified_name(parent_qualified_name: str, callable_name: str) -> str:
    """Build the canonical qualified name for a function or method.

    Args:
        parent_qualified_name: Enclosing module or class qualified name.
        callable_name: Simple declared callable name.

    Returns:
        Dotted qualified name for the callable.
    """
    if parent_qualified_name:
        return f"{parent_qualified_name}.{callable_name}"
    return callable_name


def build_nested_qualified_name(
    module_path: str,
    lexical_chain: Sequence[str],
    name: str,
) -> str:
    """Build a qualified name incorporating enclosing class/function scope names.

    Args:
        module_path: Dotted Python module path.
        lexical_chain: Ordered enclosing declaration names from outermost to innermost.
        name: Simple declared symbol name.

    Returns:
        Full dotted qualified name string.
    """
    parts = [module_path] if module_path else []
    parts.extend(lexical_chain)
    parts.append(name)
    return ".".join(parts)


def build_module_node_id(repo_id: str, module_path: str) -> str:
    """Build a deterministic node ID for a module symbol.

    Args:
        repo_id: Owning repository identifier.
        module_path: Dotted Python module path.

    Returns:
        Canonical ``sym:<repo_id>:module:<module_path>`` identifier.
    """
    return f"{PREFIX_SYMBOL}{repo_id}:{SymbolKind.MODULE.value}:{module_path}"


def build_class_node_id(repo_id: str, qualified_name: str) -> str:
    """Build a deterministic node ID for a class symbol.

    Args:
        repo_id: Owning repository identifier.
        qualified_name: Full class qualified name.

    Returns:
        Canonical ``sym:<repo_id>:class:<qualified_name>`` identifier.
    """
    return f"{PREFIX_SYMBOL}{repo_id}:{SymbolKind.CLASS.value}:{qualified_name}"


def build_callable_node_id(repo_id: str, kind: SymbolKind | str, qualified_name: str) -> str:
    """Build a deterministic node ID for a callable symbol.

    Args:
        repo_id: Owning repository identifier.
        kind: Callable ``SymbolKind`` (e.g., ``function``, ``method``, ``local_function``).
        qualified_name: Full callable qualified name.

    Returns:
        Canonical ``sym:<repo_id>:<kind>:<qualified_name>`` identifier.
    """
    kind_val = kind.value if isinstance(kind, SymbolKind) else str(kind)
    return f"{PREFIX_SYMBOL}{repo_id}:{kind_val}:{qualified_name}"


def build_assignment_node_id(repo_id: str, kind: SymbolKind | str, qualified_name: str) -> str:
    """Build a deterministic node ID for a constant, attribute, or type alias symbol.

    Args:
        repo_id: Owning repository identifier.
        kind: Assignment ``SymbolKind`` (``constant``, ``attribute``, or ``type_alias``).
        qualified_name: Full symbol qualified name.

    Returns:
        Canonical ``sym:<repo_id>:<kind>:<qualified_name>`` identifier.
    """
    kind_val = kind.value if isinstance(kind, SymbolKind) else str(kind)
    return f"{PREFIX_SYMBOL}{repo_id}:{kind_val}:{qualified_name}"


def make_structural_edge_id(
    repo_id: str,
    kind: EdgeKind | str,
    from_id: str,
    to_id: str,
) -> str:
    """Build a deterministic edge ID for structural ``contains`` or ``scope_parent`` edges.

    Args:
        repo_id: Owning repository identifier.
        kind: Structural ``EdgeKind`` (``contains`` or ``scope_parent``).
        from_id: Source symbol node identifier.
        to_id: Target symbol node identifier.

    Returns:
        Canonical ``edge:<repo_id>:<kind>:<from_id>-><to_id>`` identifier.
    """
    kind_val = kind.value if isinstance(kind, EdgeKind) else str(kind)
    return f"{PREFIX_EDGE}{repo_id}:{kind_val}:{from_id}->{to_id}"


__all__ = [
    "build_assignment_node_id",
    "build_callable_node_id",
    "build_callable_qualified_name",
    "build_class_node_id",
    "build_class_qualified_name",
    "build_module_node_id",
    "build_module_qualified_name",
    "build_nested_qualified_name",
    "make_structural_edge_id",
]
