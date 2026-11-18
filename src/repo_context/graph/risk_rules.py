"""Risk issue detection rules.

Detects issue codes from extracted risk facts.
Each issue code represents a specific risk pattern.
"""

from repo_context.graph.risk_types import RiskFacts

# All allowed issue codes
ISSUE_STALE_CONTEXT = "stale_context"
ISSUE_LOW_CONFIDENCE_MATCH = "low_confidence_match"
ISSUE_HIGH_REFERENCE_COUNT = "high_reference_count"
ISSUE_MODERATE_REFERENCE_COUNT = "moderate_reference_count"
ISSUE_CROSS_FILE_IMPACT = "cross_file_impact"
ISSUE_CROSS_MODULE_IMPACT = "cross_module_impact"
ISSUE_PUBLIC_SURFACE_CHANGE = "public_surface_change"
ISSUE_INHERITANCE_RISK = "inheritance_risk"
ISSUE_MULTI_FILE_CHANGE = "multi_file_change"
ISSUE_MULTI_MODULE_CHANGE = "multi_module_change"
ISSUE_REFERENCE_DATA_UNAVAILABLE = "reference_data_unavailable"

ALL_ISSUE_CODES = [
    ISSUE_STALE_CONTEXT,
    ISSUE_LOW_CONFIDENCE_MATCH,
    ISSUE_HIGH_REFERENCE_COUNT,
    ISSUE_MODERATE_REFERENCE_COUNT,
    ISSUE_CROSS_FILE_IMPACT,
    ISSUE_CROSS_MODULE_IMPACT,
    ISSUE_PUBLIC_SURFACE_CHANGE,
    ISSUE_INHERITANCE_RISK,
    ISSUE_MULTI_FILE_CHANGE,
    ISSUE_MULTI_MODULE_CHANGE,
    ISSUE_REFERENCE_DATA_UNAVAILABLE,
]


def _detect_reference_volume_issue(facts: RiskFacts) -> str | None:
    """Return high (15+) or moderate (5+) reference-count issue code if applicable."""
    has_moderate_refs = False
    for symbol_id, count in facts.reference_counts.items():
        if not facts.reference_availability.get(symbol_id, False):
            continue
        if count >= 15:
            return ISSUE_HIGH_REFERENCE_COUNT
        if count >= 5:
            has_moderate_refs = True
    return ISSUE_MODERATE_REFERENCE_COUNT if has_moderate_refs else None


def detect_risk_issues(facts: RiskFacts) -> list[str]:
    """Detect risk issues from extracted facts.

    Args:
        facts: Populated RiskFacts object.

    Returns:
        List of triggered issue codes in deterministic order.
    """
    ref_volume_issue = _detect_reference_volume_issue(facts)
    checks: tuple[tuple[bool, str], ...] = (
        (bool(facts.stale_symbols), ISSUE_STALE_CONTEXT),
        (
            bool(facts.low_confidence_symbols or facts.low_confidence_edges),
            ISSUE_LOW_CONFIDENCE_MATCH,
        ),
        (ref_volume_issue == ISSUE_HIGH_REFERENCE_COUNT, ISSUE_HIGH_REFERENCE_COUNT),
        (ref_volume_issue == ISSUE_MODERATE_REFERENCE_COUNT, ISSUE_MODERATE_REFERENCE_COUNT),
        (facts.cross_file_impact, ISSUE_CROSS_FILE_IMPACT),
        (facts.cross_module_impact, ISSUE_CROSS_MODULE_IMPACT),
        (facts.touches_public_surface, ISSUE_PUBLIC_SURFACE_CHANGE),
        (facts.inheritance_involved, ISSUE_INHERITANCE_RISK),
        (facts.target_spans_multiple_files, ISSUE_MULTI_FILE_CHANGE),
        (facts.target_spans_multiple_modules, ISSUE_MULTI_MODULE_CHANGE),
        (
            bool(facts.reference_availability) and not all(facts.reference_availability.values()),
            ISSUE_REFERENCE_DATA_UNAVAILABLE,
        ),
    )
    return [code for triggered, code in checks if triggered]
