"""Risk scoring and decision logic.

Computes numeric risk score from issue codes and determines final decision.
"""

from repo_context.graph.risk_rules import (
    ISSUE_CROSS_FILE_IMPACT,
    ISSUE_CROSS_MODULE_IMPACT,
    ISSUE_HIGH_REFERENCE_COUNT,
    ISSUE_INHERITANCE_RISK,
    ISSUE_LOW_CONFIDENCE_MATCH,
    ISSUE_MODERATE_REFERENCE_COUNT,
    ISSUE_MULTI_FILE_CHANGE,
    ISSUE_MULTI_MODULE_CHANGE,
    ISSUE_PUBLIC_SURFACE_CHANGE,
    ISSUE_REFERENCE_DATA_UNAVAILABLE,
    ISSUE_STALE_CONTEXT,
)
from repo_context.graph.risk_types import RiskFacts

# Decision constants
DECISION_SAFE_ENOUGH = "safe_enough"
DECISION_REVIEW_REQUIRED = "review_required"
DECISION_HIGH_RISK = "high_risk"

# Issue weights - scaled by impact severity
ISSUE_WEIGHTS = {
    # Context quality issues
    ISSUE_STALE_CONTEXT: 20,
    ISSUE_LOW_CONFIDENCE_MATCH: 20,
    ISSUE_REFERENCE_DATA_UNAVAILABLE: 15,

    # Reference count issues - scaled by usage level
    ISSUE_HIGH_REFERENCE_COUNT: 30,      # 15+ references (heavy usage)
    ISSUE_MODERATE_REFERENCE_COUNT: 15,  # 5-14 references (moderate usage)

    # Impact spread issues
    ISSUE_CROSS_FILE_IMPACT: 20,         # References from multiple files
    ISSUE_CROSS_MODULE_IMPACT: 25,       # References from multiple modules
    ISSUE_PUBLIC_SURFACE_CHANGE: 20,     # Public API affected

    # Inheritance and structural issues
    ISSUE_INHERITANCE_RISK: 15,
    ISSUE_MULTI_FILE_CHANGE: 15,         # Targets span multiple files
    ISSUE_MULTI_MODULE_CHANGE: 20,       # Targets span multiple modules
}

# Local-scope mitigation (applied if touches_local_scope_only and no public_surface_change)
LOCAL_SCOPE_MITIGATION = -10


def score_risk(issues: list[str], facts: RiskFacts) -> int:
    """Compute risk score from issue codes.

    Args:
        issues: List of triggered issue codes.
        facts: Risk facts (used for local-scope mitigation).

    Returns:
        Risk score clamped to 0-100 range.
    """
    # Sum weights of triggered issues
    score = sum(ISSUE_WEIGHTS.get(issue, 0) for issue in issues)

    # Apply local-scope mitigation if applicable
    if facts.touches_local_scope_only and not facts.touches_public_surface:
        score += LOCAL_SCOPE_MITIGATION

    # Clamp to 0-100
    return max(0, min(100, score))


_SIGNIFICANT_UNAVAILABLE_ISSUES = frozenset(
    {
        ISSUE_PUBLIC_SURFACE_CHANGE,
        ISSUE_CROSS_MODULE_IMPACT,
        ISSUE_INHERITANCE_RISK,
    }
)


def _requires_review_override(issues: list[str]) -> bool:
    """Return True if issue combinations force at least ``review_required``."""
    issue_set = set(issues)
    if ISSUE_STALE_CONTEXT in issue_set:
        return True
    if ISSUE_LOW_CONFIDENCE_MATCH in issue_set and len(issue_set) > 1:
        return True
    return bool(
        ISSUE_REFERENCE_DATA_UNAVAILABLE in issue_set
        and issue_set & _SIGNIFICANT_UNAVAILABLE_ISSUES
    )


def decide_risk(issues: list[str], facts: RiskFacts, score: int) -> str:
    """Determine final risk decision.

    Base thresholds:
    - 0-29: safe_enough
    - 30-69: review_required
    - 70-100: high_risk

    Override rules:
    - stale_context -> at least review_required
    - low_confidence_match + another issue -> at least review_required
    - reference_data_unavailable + (public_surface OR cross_module OR inheritance)
      -> at least review_required

    Args:
        issues: List of triggered issue codes.
        facts: Risk facts.
        score: Computed risk score.

    Returns:
        Decision string (safe_enough, review_required, high_risk).
    """
    if score >= 70:
        return DECISION_HIGH_RISK
    if score >= 30 or _requires_review_override(issues):
        return DECISION_REVIEW_REQUIRED
    return DECISION_SAFE_ENOUGH
