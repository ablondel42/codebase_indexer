"""Deterministic change-risk issue detection, weighted scoring, and decision rules."""

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from graph.models.risk import RiskDecision, RiskFacts, RiskIssueCode

ISSUE_WEIGHTS: Mapping[str, int] = MappingProxyType({
    RiskIssueCode.STALE_CONTEXT.value: 20,
    RiskIssueCode.LOW_CONFIDENCE_MATCH.value: 20,
    RiskIssueCode.REFERENCE_DATA_UNAVAILABLE.value: 15,
    RiskIssueCode.HIGH_REFERENCE_COUNT.value: 30,
    RiskIssueCode.MODERATE_REFERENCE_COUNT.value: 15,
    RiskIssueCode.CROSS_FILE_IMPACT.value: 20,
    RiskIssueCode.CROSS_MODULE_IMPACT.value: 25,
    RiskIssueCode.PUBLIC_SURFACE_CHANGE.value: 20,
    RiskIssueCode.INHERITANCE_RISK.value: 15,
    RiskIssueCode.MULTI_FILE_CHANGE.value: 15,
    RiskIssueCode.MULTI_MODULE_CHANGE.value: 20,
})

LOCAL_SCOPE_MITIGATION: int = -10

_SIGNIFICANT_UNAVAILABLE_ISSUES: frozenset[str] = frozenset({
    RiskIssueCode.PUBLIC_SURFACE_CHANGE.value,
    RiskIssueCode.CROSS_MODULE_IMPACT.value,
    RiskIssueCode.INHERITANCE_RISK.value,
})


def _detect_reference_volume_issue(facts: RiskFacts) -> str | None:
    """Return high (``15+``) or moderate (``5..14``) LSP reference volume issue code.

    Args:
        facts: Extracted ``RiskFacts`` containing counts and availability flags.

    Returns:
        ``"high_reference_count"``, ``"moderate_reference_count"``, or ``None``.
    """
    has_moderate_refs = False
    for symbol_id, count in facts.reference_counts.items():
        if not facts.reference_availability.get(symbol_id, False):
            continue
        if count >= 15:
            return RiskIssueCode.HIGH_REFERENCE_COUNT.value
        if count >= 5:
            has_moderate_refs = True
    return RiskIssueCode.MODERATE_REFERENCE_COUNT.value if has_moderate_refs else None


def detect_risk_issues(facts: RiskFacts) -> list[str]:
    """Detect triggered ``RiskIssueCode`` values in canonical deterministic order.

    Args:
        facts: Populated ``RiskFacts`` model.

    Returns:
        Ordered list of triggered issue code strings.
    """
    ref_volume_issue = _detect_reference_volume_issue(facts)
    checks: tuple[tuple[bool, str], ...] = (
        (bool(facts.stale_symbols), RiskIssueCode.STALE_CONTEXT.value),
        (
            bool(facts.low_confidence_symbols or facts.low_confidence_edges),
            RiskIssueCode.LOW_CONFIDENCE_MATCH.value,
        ),
        (
            ref_volume_issue == RiskIssueCode.HIGH_REFERENCE_COUNT.value,
            RiskIssueCode.HIGH_REFERENCE_COUNT.value,
        ),
        (
            ref_volume_issue == RiskIssueCode.MODERATE_REFERENCE_COUNT.value,
            RiskIssueCode.MODERATE_REFERENCE_COUNT.value,
        ),
        (facts.cross_file_impact, RiskIssueCode.CROSS_FILE_IMPACT.value),
        (facts.cross_module_impact, RiskIssueCode.CROSS_MODULE_IMPACT.value),
        (facts.touches_public_surface, RiskIssueCode.PUBLIC_SURFACE_CHANGE.value),
        (facts.inheritance_involved, RiskIssueCode.INHERITANCE_RISK.value),
        (facts.target_spans_multiple_files, RiskIssueCode.MULTI_FILE_CHANGE.value),
        (facts.target_spans_multiple_modules, RiskIssueCode.MULTI_MODULE_CHANGE.value),
        (
            bool(facts.reference_availability) and not all(facts.reference_availability.values()),
            RiskIssueCode.REFERENCE_DATA_UNAVAILABLE.value,
        ),
    )
    return [code for triggered, code in checks if triggered]


def score_risk(issues: Sequence[str], facts: RiskFacts) -> int:
    """Compute a ``0..100`` numeric risk score from triggered ``issues`` and ``facts``.

    Applies ``LOCAL_SCOPE_MITIGATION`` (``-10``) when all targets are function-local
    and do not touch any public surface.

    Args:
        issues: Sequence of triggered issue code strings.
        facts: Extracted ``RiskFacts`` model.

    Returns:
        Clamped integer risk score in ``[0, 100]``.
    """
    score = sum(ISSUE_WEIGHTS.get(str(issue), 0) for issue in issues)
    if facts.touches_local_scope_only and not facts.touches_public_surface:
        score += LOCAL_SCOPE_MITIGATION
    return max(0, min(100, score))


def _requires_review_override(issues: Sequence[str]) -> bool:
    """Return ``True`` when specific issue combinations force at least ``REVIEW_REQUIRED``.

    Args:
        issues: Sequence of triggered issue code strings.

    Returns:
        ``True`` if an override rule elevates ``SAFE_ENOUGH`` to ``REVIEW_REQUIRED``.
    """
    issue_set = {str(item) for item in issues}
    if RiskIssueCode.STALE_CONTEXT.value in issue_set:
        return True
    if RiskIssueCode.LOW_CONFIDENCE_MATCH.value in issue_set and len(issue_set) > 1:
        return True
    return bool(
        RiskIssueCode.REFERENCE_DATA_UNAVAILABLE.value in issue_set
        and (issue_set & _SIGNIFICANT_UNAVAILABLE_ISSUES)
    )


def decide_risk(issues: Sequence[str], risk_score: int) -> RiskDecision:
    """Determine the final ``RiskDecision`` from ``issues`` and ``risk_score``."""
    if risk_score >= 70:
        return RiskDecision.HIGH_RISK
    if risk_score >= 30 or _requires_review_override(issues):
        return RiskDecision.REVIEW_REQUIRED
    return RiskDecision.SAFE_ENOUGH


__all__ = [
    "ISSUE_WEIGHTS",
    "LOCAL_SCOPE_MITIGATION",
    "decide_risk",
    "detect_risk_issues",
    "score_risk",
]
