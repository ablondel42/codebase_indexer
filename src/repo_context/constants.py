"""Constants and enums for the application."""

from enum import Enum


class Language(str, Enum):
    """Supported programming languages."""

    PYTHON = "python"


# Symbol kind constants
SYMBOL_KIND_MODULE = "module"
SYMBOL_KIND_CLASS = "class"
SYMBOL_KIND_FUNCTION = "function"
SYMBOL_KIND_ASYNC_FUNCTION = "async_function"
SYMBOL_KIND_METHOD = "method"
SYMBOL_KIND_ASYNC_METHOD = "async_method"
SYMBOL_KIND_LOCAL_FUNCTION = "local_function"
SYMBOL_KIND_LOCAL_ASYNC_FUNCTION = "local_async_function"
SYMBOL_KIND_CONSTANT = "constant"
SYMBOL_KIND_ATTRIBUTE = "attribute"
SYMBOL_KIND_TYPE_ALIAS = "type_alias"

# Valid symbol kinds
VALID_SYMBOL_KINDS = {
    SYMBOL_KIND_MODULE,
    SYMBOL_KIND_CLASS,
    SYMBOL_KIND_FUNCTION,
    SYMBOL_KIND_ASYNC_FUNCTION,
    SYMBOL_KIND_METHOD,
    SYMBOL_KIND_ASYNC_METHOD,
    SYMBOL_KIND_LOCAL_FUNCTION,
    SYMBOL_KIND_LOCAL_ASYNC_FUNCTION,
    SYMBOL_KIND_CONSTANT,
    SYMBOL_KIND_ATTRIBUTE,
    SYMBOL_KIND_TYPE_ALIAS,
}

# Valid scopes for symbols
VALID_SCOPES = {"module", "class", "function"}

# Edge kind constants
EDGE_KIND_CONTAINS = "contains"
EDGE_KIND_IMPORTS = "imports"
EDGE_KIND_INHERITS = "inherits"
EDGE_KIND_SCOPE_PARENT = "scope_parent"
EDGE_KIND_CALLS = "calls"
EDGE_KIND_REFERENCES = "references"

# Valid edge kinds
VALID_EDGE_KINDS = {
    EDGE_KIND_CONTAINS,
    EDGE_KIND_IMPORTS,
    EDGE_KIND_INHERITS,
    EDGE_KIND_SCOPE_PARENT,
    EDGE_KIND_CALLS,
    EDGE_KIND_REFERENCES,
}

# ID format prefixes
PREFIX_REPO = "repo:"
PREFIX_FILE = "file:"
PREFIX_SYMBOL = "sym:"
PREFIX_EDGE = "edge:"

# Hash prefixes
PREFIX_SHA256 = "sha256:"

# URI schemes
SCHEME_FILE = "file://"

# Default values
DEFAULT_LANGUAGE = Language.PYTHON
DEFAULT_SOURCE = "python-ast"
DEFAULT_CONFIDENCE = 1.0
