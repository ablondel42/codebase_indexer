"""Parsing layer for repository scanning, AST extraction, and intra-repo linking."""

from repo_context.parsing.assignment_extractor import extract_assignment_nodes
from repo_context.parsing.ast_loader import load_file_text, parse_file
from repo_context.parsing.call_extractor import extract_call_edges
from repo_context.parsing.callable_extractor import extract_callable_nodes, extract_parameters
from repo_context.parsing.class_extractor import extract_class_nodes
from repo_context.parsing.docstrings import get_doc_summary
from repo_context.parsing.hashing import (
    compute_symbol_content_hash,
    sha256_file,
    sha256_text,
)
from repo_context.parsing.import_extractor import extract_import_edges_and_payload
from repo_context.parsing.inheritance_extractor import extract_inheritance_edges
from repo_context.parsing.linker import resolve_repo_edges
from repo_context.parsing.module_extractor import extract_module_node
from repo_context.parsing.naming import (
    DuplicateTracker,
    build_callable_node_id,
    build_callable_qualified_name,
    build_class_node_id,
    build_class_qualified_name,
    build_module_node_id,
    build_module_qualified_name,
    build_nested_qualified_name,
    cap_signature,
    compute_visibility_hint,
)
from repo_context.parsing.pathing import (
    derive_file_id,
    derive_module_path,
    derive_repo_id,
    ensure_safe_repo_file,
    normalize_repo_root,
    to_file_uri,
    to_relative_path,
)
from repo_context.parsing.pipeline import extract_file_graph
from repo_context.parsing.ranges import (
    make_declaration_lines,
    make_name_selection_range,
    make_position,
    make_range,
    to_zero_based_line,
    utf8_byte_offset_to_char_offset,
)
from repo_context.parsing.scanner import (
    build_file_record,
    build_repo_record,
    is_supported_source_file,
    is_test_path,
    scan_repository,
    should_ignore_dir,
)
from repo_context.parsing.scope_tracker import ScopeTracker

__all__ = [
    # Pathing & security
    "normalize_repo_root",
    "ensure_safe_repo_file",
    "derive_repo_id",
    "derive_file_id",
    "to_relative_path",
    "to_file_uri",
    "derive_module_path",
    # Scanner
    "should_ignore_dir",
    "is_test_path",
    "is_supported_source_file",
    "build_repo_record",
    "build_file_record",
    "scan_repository",
    # Hashing
    "sha256_text",
    "sha256_file",
    "compute_symbol_content_hash",
    # AST loader
    "load_file_text",
    "parse_file",
    # Naming
    "build_module_qualified_name",
    "build_class_qualified_name",
    "build_callable_qualified_name",
    "build_module_node_id",
    "build_class_node_id",
    "build_callable_node_id",
    "build_nested_qualified_name",
    "cap_signature",
    "compute_visibility_hint",
    "DuplicateTracker",
    # Ranges
    "to_zero_based_line",
    "make_position",
    "make_range",
    "make_name_selection_range",
    "make_declaration_lines",
    "utf8_byte_offset_to_char_offset",
    # Docstrings
    "get_doc_summary",
    # Extractors
    "extract_module_node",
    "extract_class_nodes",
    "extract_callable_nodes",
    "extract_parameters",
    "extract_assignment_nodes",
    "extract_import_edges_and_payload",
    "extract_inheritance_edges",
    "extract_call_edges",
    # Pipeline & Linker
    "extract_file_graph",
    "resolve_repo_edges",
    # Scope tracker
    "ScopeTracker",
]
