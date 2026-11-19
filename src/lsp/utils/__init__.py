"""Public re-exports for stateless ``lsp.utils`` helper functions."""

from lsp.utils.framing import (
    encode_jsonrpc_message,
    parse_content_length_header,
    parse_header_line,
)
from lsp.utils.locations import (
    CONFIDENCE_EXACT,
    CONFIDENCE_MODULE,
    build_reference_edge,
    extract_uri_candidates,
    find_module_node_for_file,
    get_reference_query_position,
    normalize_location,
    pick_smallest_containing_symbol,
    range_contains,
)
from lsp.utils.utf16 import (
    codepoint_offset_to_utf16,
    position_codepoint_to_utf16,
    position_utf16_to_codepoint,
    range_utf16_to_codepoint,
    utf16_offset_to_codepoint,
)

__all__ = [
    "CONFIDENCE_EXACT",
    "CONFIDENCE_MODULE",
    "build_reference_edge",
    "codepoint_offset_to_utf16",
    "encode_jsonrpc_message",
    "extract_uri_candidates",
    "find_module_node_for_file",
    "get_reference_query_position",
    "normalize_location",
    "parse_content_length_header",
    "parse_header_line",
    "pick_smallest_containing_symbol",
    "position_codepoint_to_utf16",
    "position_utf16_to_codepoint",
    "range_contains",
    "range_utf16_to_codepoint",
    "utf16_offset_to_codepoint",
]
