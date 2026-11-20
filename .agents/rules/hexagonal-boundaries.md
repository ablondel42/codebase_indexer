---
trigger: glob
globs: "**/src/**/*.py, **/tests/**/*.py"
---

# Hexagonal Architecture & Hygiene Boundaries

All Python files under `src/` and `tests/` must satisfy `TestHexagonalArchitectureBoundaryGate` in `tests/test_engine_and_architecture.py` and the Ruff/Pyright settings in `pyproject.toml`.

## 1. 11-Package Hexagonal Layout (`src/<domain>/`)
- **Exact top-level packages**: `core`, `storage`, `lsp`, `parsing`, `graph`, `context`, `views`, `indexing`, `engine`, `mcp_server`, `cli`. Do not add extra top-level packages under `src/`.
- **Subpackages**: `core/{ports,models,utils}`; all other 10 domains strictly `{services,models,utils}`.
- **Package root `.py` files**: only `__init__.py` (plus `src/cli/main.py`).
- **Naming**: zero `*_service.py` filenames anywhere in `src/`.

## 2. Strict Allowed-Import Matrix
Stdlib modules (except `sqlite3`) are allowed everywhere. Internal and third-party imports are restricted per domain:

| Domain | Allowed Internal & Third-Party Imports |
| :--- | :--- |
| `core` | `core`, `pydantic` |
| `storage` | `core`, `storage`, `pydantic`, `sqlalchemy`, `sqlite3` *(100% of SQL/SQLite/SQLAlchemy quarantined here)* |
| `lsp` | `core`, `lsp`, `pydantic` |
| `parsing` | `core`, `parsing`, `pydantic` |
| `graph` | `core`, `graph`, `pydantic` |
| `context` | `core`, `graph`, `context`, `pydantic` |
| `views` | `core`, `views`, `pydantic` |
| `indexing` | `core`, `parsing`, `lsp`, `indexing`, `pydantic`, `watchdog` |
| `engine` | `core`, `storage`, `lsp`, `parsing`, `graph`, `context`, `views`, `indexing`, `engine`, `pydantic` |
| `mcp_server` | `core`, `engine`, `graph.models`, `context.models`, `views.models`, `indexing.models`, `mcp_server`, `pydantic`, `mcp` |
| `cli` | `core`, `engine`, `graph.models`, `context.models`, `views.models`, `indexing.models`, `cli`, `pydantic` |

- **Sole bridge exception**: `cli/services/server.py` may also import `mcp_server`.
- **Subpackage restriction**: `mcp_server` and `cli` may only import `.models` from `graph`, `context`, `views`, and `indexing` (never `services/` or `utils/`).

## 3. Imports, Layers, Models & Typing
- **Unconditional top-level absolute imports only** (`PLC0415` enforced):
  - Zero inside-function/conditional imports, zero relative imports (`from .foo`), zero `if TYPE_CHECKING:`, zero `try/except ImportError`, zero `# noqa: F401`.
- **`models/`**: Every class must inherit `FrozenModel`, `MutableModel`, or `StrEnum` (except `core/models/base.py` and `core/models/exceptions.py`), with `extra="forbid"` by default. Zero `@dataclass` or `TypedDict` anywhere in `src/`.
- **`utils/`**: Stateless pure functions only — zero classes (sole exception: `JsonText` in `storage/utils/schema.py`).
- **`services/`**: Classes only (zero top-level `def`/`async def`, zero mutable module globals). Public methods in domain service packages (`storage`, `parsing`, `lsp`, `graph`, `context`, `views`, `indexing`, `engine`) must return typed models/primitives, never `dict` (except `dict[str, FileRecord]`, `dict[str, SymbolNode]`, `dict[str, SymbolSummary]`).
- **Context managers**: `@contextmanager` must annotate `-> Generator[YieldT, None, None]` and `@asynccontextmanager` `-> AsyncGenerator[YieldT, None]` (`Iterator` is deprecated under `reportDeprecated = "error"`).

## 4. Error Handling & Complexity Caps
- **Zero silent errors**: Zero `contextlib.suppress`, zero bare `except:`. Every `except` handler must either raise (`from exc`) or log via `get_logger(...)`.
- **Quarantined broad `except Exception` / `BaseException`**: Allowed ONLY inside these 7 boundary files:
  `cli/services/app.py`, `indexing/services/batch.py`, `indexing/services/incremental.py`, `indexing/services/scheduler.py`, `lsp/services/transport.py`, `mcp_server/services/registry.py`, `storage/services/uow.py`.
- **Ruff caps (`src/`)**: `line-length=100`, `max-complexity=8`, `max-statements=35`, `max-branches=8`, `max-returns=5`, `max-args=6`, `max-locals=12`, `max-nested-blocks=3`.
