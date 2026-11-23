"""Mock Python module covering AST symbol, edge, and local-noise cases for parser testing."""

import asyncio
import pathlib as pl
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Self, TypeAlias

from ..storage import db

__all__ = [
    "DEFAULT_RETRIES",
    "Payload",
    "QuotaExceededError",
    "SERVICE_NAME",
    "TaskRecord",
    "TaskWorker",
    "build_worker",
    "run_batch",
]

# --- 1. Type Aliases & Module-Level Globals / Constants ---
Payload: TypeAlias = dict[str, Any]

DEFAULT_RETRIES = 3
SERVICE_NAME: str = "cbi-mock"
STORAGE_BACKEND: str = db.__name__
ROUTES: dict[str, Callable[..., Any]] = {}


# --- 2. Decorator Helper (simulates framework entry points like @app.get) ---
def route(path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        ROUTES[path] = fn
        return fn

    return register


# --- 3. Custom Exception (ClassDef + inheritance + raise/catch target) ---
class QuotaExceededError(RuntimeError):
    """Raised when a task or batch exceeds allowed limits."""


# --- 4. Dataclass / Schema Model (ClassDef + Call decorator + AnnAssign fields) ---
@dataclass(slots=True)
class TaskRecord:
    task_id: int
    title: str
    tags: list[str] = field(default_factory=list)


# --- 5. Base Class & Subclass (class attrs, dunder, class/static/property, sync & async) ---
class BaseWorker:
    kind = "base"

    def describe(self) -> str:
        return f"{self.kind}:{STORAGE_BACKEND}"


class TaskWorker(BaseWorker):
    kind = "task"

    def __init__(self, root: pl.Path, max_retries: int = DEFAULT_RETRIES) -> None:
        self.root: pl.Path = root
        self.max_retries: int = max_retries
        self._processed: int = 0

    @classmethod
    def from_cwd(cls) -> Self:
        return cls(root=pl.Path.cwd())

    @staticmethod
    def normalize_title(raw: str) -> str:
        return raw.strip().lower()

    @property
    def is_ready(self) -> bool:
        return self.max_retries > 0 and self.root.exists()

    def process(self, record: TaskRecord) -> Payload:
        # Local noise (should NOT be indexed as top-level/module symbols):
        # local Assign, list comprehension var, and walrus NamedExpr.
        cleaned_title = self.normalize_title(record.title)
        clean_tags = [tag.strip() for tag in record.tags if tag.strip()]
        if (tag_count := len(clean_tags)) > 10:
            raise QuotaExceededError(f"Too many tags: {tag_count}")

        self._processed += 1
        return {
            "id": record.task_id,
            "title": cleaned_title,
            "tags": clean_tags,
            "service": SERVICE_NAME,
        }

    async def process_async(self, record: TaskRecord) -> Payload:
        await asyncio.sleep(0)
        return self.process(record)


# --- 6. Top-Level Sync, Async, Decorated Entry Point & Nested Function ---
def build_worker(raw_dir: str = ".") -> TaskWorker:
    resolved = pl.Path(raw_dir).resolve()
    return TaskWorker(root=resolved, max_retries=DEFAULT_RETRIES)


@route("/tasks/batch")
async def run_batch(records: list[TaskRecord]) -> list[Payload]:
    worker = build_worker()

    def format_error(err: QuotaExceededError) -> Payload:
        return {"error": str(err), "worker": worker.describe()}

    results: list[Payload] = []
    for rec in records:
        try:
            results.append(await worker.process_async(rec))
        except QuotaExceededError as exc:
            results.append(format_error(exc))
    return results
