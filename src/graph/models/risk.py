"""Change-impact risk issue codes, decisions, targets, facts, and assessment models."""

from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from core.models.base import FrozenModel
from core.models.enums import SymbolKind


class RiskIssueCode(StrEnum):
    """Canonical deterministic risk issue identifiers."""

    STALE_CONTEXT = "stale_context"
    LOW_CONFIDENCE_MATCH = "low_confidence_match"
    HIGH_REFERENCE_COUNT = "high_reference_count"
    MODERATE_REFERENCE_COUNT = "moderate_reference_count"
    CROSS_FILE_IMPACT = "cross_file_impact"
    CROSS_MODULE_IMPACT = "cross_module_impact"
    PUBLIC_SURFACE_CHANGE = "public_surface_change"
    INHERITANCE_RISK = "inheritance_risk"
    MULTI_FILE_CHANGE = "multi_file_change"
    MULTI_MODULE_CHANGE = "multi_module_change"
    REFERENCE_DATA_UNAVAILABLE = "reference_data_unavailable"


ALL_ISSUE_CODES: tuple[str, ...] = tuple(code.value for code in RiskIssueCode)


class RiskDecision(StrEnum):
    """Final recommended review posture for a change-risk assessment."""

    SAFE_ENOUGH = "safe_enough"
    REVIEW_REQUIRED = "review_required"
    HIGH_RISK = "high_risk"


class RiskTarget(FrozenModel):
    """Normalized symbol declaration target evaluated by the risk engine."""

    symbol_id: str
    qualified_name: str
    kind: SymbolKind
    scope: str
    file_id: str
    file_path: str
    module_path: str
    location: str | None = None
    visibility_hint: str | None = None
    parent_id: str | None = None
    lexical_parent_id: str | None = None
    is_test: bool = False
    start_line: int | None = None
    end_line: int | None = None
    signature: str | None = None
    confidence: float = 1.0
    last_indexed_at: str | None = None
    repo_id: str | None = None


class RiskFacts(FrozenModel):
    """Deterministic structural, fanout, freshness, and confidence facts for risk scoring."""

    target_count: int = 0
    symbol_ids: list[str] = Field(default_factory=list)
    symbol_kinds: list[str] = Field(default_factory=list)
    reference_counts: dict[str, int] = Field(default_factory=dict)
    reference_availability: dict[str, bool] = Field(default_factory=dict)
    referencing_file_counts: dict[str, int] = Field(default_factory=dict)
    referencing_module_counts: dict[str, int] = Field(default_factory=dict)
    touches_public_surface: bool = False
    touches_local_scope_only: bool = False
    target_spans_multiple_files: bool = False
    target_spans_multiple_modules: bool = False
    cross_file_impact: bool = False
    cross_module_impact: bool = False
    inheritance_involved: bool = False
    stale_symbols: list[str] = Field(default_factory=list)
    low_confidence_symbols: list[str] = Field(default_factory=list)
    low_confidence_edges: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol_kinds", mode="before")
    @classmethod
    def _normalize_symbol_kinds(cls, value: Any) -> Any:
        """Coerce sets, tuples, or enum iterables into a sorted JSON-ready list of strings.

        Args:
            value: Raw ``symbol_kinds`` input value.

        Returns:
            Sorted deduplicated ``list[str]`` when ``value`` is a non-string iterable,
            otherwise ``value`` unchanged for Pydantic validation.
        """
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
            return sorted({str(item) for item in value if item is not None})
        return value


class TargetReferenceAggregation(FrozenModel):
    """Aggregated per-target reference fanout maps and impact-spread flags."""

    reference_counts: dict[str, int] = Field(default_factory=dict)
    reference_availability: dict[str, bool] = Field(default_factory=dict)
    referencing_file_counts: dict[str, int] = Field(default_factory=dict)
    referencing_module_counts: dict[str, int] = Field(default_factory=dict)
    reference_sources: dict[str, str] = Field(default_factory=dict)
    cross_file_impact: bool = False
    cross_module_impact: bool = False


class RiskAssessment(FrozenModel):
    """Complete risk analysis report combining targets, facts, issues, score, and decision."""

    targets: list[RiskTarget] = Field(default_factory=list)
    facts: RiskFacts = Field(default_factory=RiskFacts)
    issues: list[str] = Field(default_factory=list)
    risk_score: int = Field(default=0, ge=0, le=100)
    decision: RiskDecision = RiskDecision.SAFE_ENOUGH


__all__ = [
    "ALL_ISSUE_CODES",
    "RiskAssessment",
    "RiskDecision",
    "RiskFacts",
    "RiskIssueCode",
    "RiskTarget",
    "TargetReferenceAggregation",
]
