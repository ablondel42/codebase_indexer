"""Canonical string enumerations, grouped kind sets, and identifier constants."""

from enum import StrEnum

APP_VERSION = "0.2.0"


class Language(StrEnum):
    """Supported programming languages."""

    PYTHON = "python"


class SymbolKind(StrEnum):
    """Canonical symbol declaration kinds in the repository graph."""

    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    ASYNC_FUNCTION = "async_function"
    METHOD = "method"
    ASYNC_METHOD = "async_method"
    LOCAL_FUNCTION = "local_function"
    LOCAL_ASYNC_FUNCTION = "local_async_function"
    CONSTANT = "constant"
    ATTRIBUTE = "attribute"
    TYPE_ALIAS = "type_alias"


class SymbolScope(StrEnum):
    """Enclosing declaration scope for a symbol."""

    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"


class EdgeKind(StrEnum):
    """Canonical relationship kinds between symbol nodes."""

    CONTAINS = "contains"
    IMPORTS = "imports"
    INHERITS = "inherits"
    SCOPE_PARENT = "scope_parent"
    CALLS = "calls"
    REFERENCES = "references"


class FreshnessStatus(StrEnum):
    """On-disk freshness verification outcomes for tracked files."""

    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    SECURITY_VIOLATION = "security_violation"


class RefreshStatus(StrEnum):
    """LSP reference refresh outcomes for tracked symbols."""

    OK = "ok"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


CALLABLE_KINDS: frozenset[SymbolKind] = frozenset(
    {
        SymbolKind.FUNCTION,
        SymbolKind.ASYNC_FUNCTION,
        SymbolKind.METHOD,
        SymbolKind.ASYNC_METHOD,
        SymbolKind.LOCAL_FUNCTION,
        SymbolKind.LOCAL_ASYNC_FUNCTION,
    }
)

LOCAL_CALLABLE_KINDS: frozenset[SymbolKind] = frozenset(
    {
        SymbolKind.LOCAL_FUNCTION,
        SymbolKind.LOCAL_ASYNC_FUNCTION,
    }
)

AST_DEPENDENCY_EDGE_KINDS: tuple[EdgeKind, ...] = (
    EdgeKind.CALLS,
    EdgeKind.IMPORTS,
    EdgeKind.INHERITS,
)

REFERENCE_AND_DEPENDENCY_EDGE_KINDS: tuple[EdgeKind, ...] = (
    EdgeKind.REFERENCES,
    EdgeKind.CALLS,
    EdgeKind.IMPORTS,
    EdgeKind.INHERITS,
)

PREFIX_REPO = "repo:"
PREFIX_FILE = "file:"
PREFIX_SYMBOL = "sym:"
PREFIX_EDGE = "edge:"
PREFIX_SHA256 = "sha256:"
SCHEME_FILE = "file://"

DEFAULT_LANGUAGE = Language.PYTHON
DEFAULT_SOURCE = "python-ast"
DEFAULT_CONFIDENCE = 1.0
