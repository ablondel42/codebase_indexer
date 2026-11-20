"""Typed collaborator dependency bundles for ``CommandEngine`` and ``QueryEngine``."""

from pydantic import ConfigDict

from context.services.builder import SymbolContextBuilder
from context.services.code_search import CodeSearchService
from context.services.outline import OutlineBuilder
from context.services.source import SourceReader
from core.models.base import FrozenModel
from graph.services.navigator import GraphNavigator
from graph.services.references import ReferenceAnalyzer
from graph.services.risk import RiskAnalyzer
from indexing.services.batch import BatchIndexer
from indexing.services.incremental import IncrementalIndexer
from indexing.services.lsp_runner import RepoLspCoordinator
from indexing.services.watcher import RepoWatcher


class CommandCollaborators(FrozenModel):
    """Optional write-side domain service overrides for ``CommandEngine``."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    batch_indexer: BatchIndexer | None = None
    incremental_indexer: IncrementalIndexer | None = None
    lsp_coordinator: RepoLspCoordinator | None = None
    watcher: RepoWatcher | None = None


class QueryCollaborators(FrozenModel):
    """Optional read-side domain service overrides for ``QueryEngine``."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    navigator: GraphNavigator | None = None
    reference_analyzer: ReferenceAnalyzer | None = None
    risk_analyzer: RiskAnalyzer | None = None
    source_reader: SourceReader | None = None
    outline_builder: OutlineBuilder | None = None
    context_builder: SymbolContextBuilder | None = None
    code_search: CodeSearchService | None = None


__all__ = [
    "CommandCollaborators",
    "QueryCollaborators",
]
