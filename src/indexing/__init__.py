"""Write-side indexing, linking, LSP refresh, debounced scheduling, and watch domain."""

from indexing.models.events import (
    FileChangeEvent,
    FileEventType,
    NormalizeContext,
    WatchFileProcessedEvent,
    WatchStartedEvent,
    WatchStoppedEvent,
)
from indexing.models.results import (
    BatchIndexOptions,
    BatchIndexResult,
    ExtractionFailureEntry,
    IncrementalBatchResult,
    IncrementalFileResult,
    IncrementalStatus,
    LspBatchSummary,
)
from indexing.services.batch import BatchIndexer
from indexing.services.directory_watcher import DirectoryTreeWatcher, DirectoryWatchHandle
from indexing.services.incremental import IncrementalIndexer
from indexing.services.linker_runner import RepoLinkCoordinator
from indexing.services.lsp_runner import RepoLspCoordinator
from indexing.services.normalizer import WatchEventNormalizer
from indexing.services.scheduler import EventScheduler
from indexing.services.watcher import RepoWatcher

__all__ = [
    "BatchIndexOptions",
    "BatchIndexResult",
    "BatchIndexer",
    "DirectoryTreeWatcher",
    "DirectoryWatchHandle",
    "EventScheduler",
    "ExtractionFailureEntry",
    "FileChangeEvent",
    "FileEventType",
    "IncrementalBatchResult",
    "IncrementalFileResult",
    "IncrementalIndexer",
    "IncrementalStatus",
    "LspBatchSummary",
    "NormalizeContext",
    "RepoLinkCoordinator",
    "RepoLspCoordinator",
    "RepoWatcher",
    "WatchEventNormalizer",
    "WatchFileProcessedEvent",
    "WatchStartedEvent",
    "WatchStoppedEvent",
]
