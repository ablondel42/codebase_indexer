"""Deterministic change-impact risk analysis orchestration service."""

from collections.abc import Sequence

from core.models.config import AppConfig
from core.ports.uow import UnitOfWorkProtocol
from core.utils.logging import get_logger
from graph.models.risk import RiskAssessment
from graph.services.risk_facts import RiskFactCollector
from graph.utils.scoring import decide_risk, detect_risk_issues, score_risk

logger = get_logger("graph.services.risk")


class RiskAnalyzer:
    """Orchestrates target loading, fact extraction, issue detection, and risk scoring."""

    def __init__(
        self,
        config: AppConfig | None = None,
        fact_collector: RiskFactCollector | None = None,
    ) -> None:
        """Initialize the analyzer with optional configuration and fact collector.

        Args:
            config: Optional ``AppConfig`` instance.
            fact_collector: Optional ``RiskFactCollector`` collaborator.
        """
        self._config = config if config is not None else AppConfig()
        self._collector = (
            fact_collector if fact_collector is not None else RiskFactCollector(self._config)
        )

    def analyze_symbol(
        self,
        uow: UnitOfWorkProtocol,
        symbol_id: str,
    ) -> RiskAssessment:
        """Analyze change-impact risk for a single symbol identifier.

        Args:
            uow: Active read Unit of Work.
            symbol_id: Canonical target symbol identifier.

        Returns:
            Populated ``RiskAssessment`` report.

        Raises:
            ValidationError: If ``symbol_id`` is blank.
            SymbolNotFoundError: If ``symbol_id`` does not exist in the graph.
        """
        return self.analyze_targets(uow, [symbol_id])

    def analyze_targets(
        self,
        uow: UnitOfWorkProtocol,
        symbol_ids: Sequence[str],
    ) -> RiskAssessment:
        """Analyze combined change-impact risk across one or more target symbol IDs.

        Args:
            uow: Active read Unit of Work.
            symbol_ids: Sequence of canonical target symbol identifiers.

        Returns:
            Populated ``RiskAssessment`` report.

        Raises:
            ValidationError: If ``symbol_ids`` is empty or blank.
            SymbolNotFoundError: If any requested symbol ID does not exist in the graph.
        """
        targets = self._collector.load_targets(uow, symbol_ids)
        facts = self._collector.collect_facts(uow, targets)
        issues = detect_risk_issues(facts)
        risk_score = score_risk(issues, facts)
        decision = decide_risk(issues, facts, risk_score)
        logger.debug(
            "Analyzed risk for %d target(s): score=%d decision=%s issues=%s",
            len(targets),
            risk_score,
            decision,
            issues,
        )
        return RiskAssessment(
            targets=targets,
            facts=facts,
            issues=issues,
            risk_score=risk_score,
            decision=decision,
        )


__all__ = [
    "RiskAnalyzer",
]
