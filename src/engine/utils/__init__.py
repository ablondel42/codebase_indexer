"""Stateless formatting, path normalization, and file pattern utilities for ``engine``."""

from engine.utils.formatting import (
    aggregate_top_directories,
    format_cli_freshness_label,
    matches_file_pattern,
    relativize_if_under_root,
)
from engine.utils.projections import (
    derive_symbol_summary_lines,
    omit_none,
    project_code_match,
    project_code_search,
    project_reference_site,
    project_risk_assessment,
    project_risk_target,
    project_symbol_references,
    project_symbol_search,
    project_symbol_source,
    project_symbol_summary,
)

__all__ = [
    "aggregate_top_directories",
    "derive_symbol_summary_lines",
    "format_cli_freshness_label",
    "matches_file_pattern",
    "omit_none",
    "project_code_match",
    "project_code_search",
    "project_reference_site",
    "project_risk_assessment",
    "project_risk_target",
    "project_symbol_references",
    "project_symbol_search",
    "project_symbol_source",
    "project_symbol_summary",
    "relativize_if_under_root",
]
