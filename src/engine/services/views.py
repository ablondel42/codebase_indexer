"""Side-effect-free read view engine over indexed repository state."""

from core.models.config import AppConfig
from core.models.exceptions import DatabaseError
from core.models.repo import RepoRecord
from core.ports.uow import UnitOfWorkFactoryProtocol, UnitOfWorkProtocol
from core.utils.logging import get_logger
from engine.models.collaborators import ViewCollaborators
from engine.utils.formatting import relativize_if_under_root
from views.models.filesystem import FilesystemView, FilesystemViewQuery
from views.services.filesystem import FilesystemViewBuilder

logger = get_logger("engine.services.views")


class ViewEngine:
    """Executes read-only project view projections for CLI and UI consumers."""

    def __init__(
        self,
        config: AppConfig,
        uow_factory: UnitOfWorkFactoryProtocol,
        *,
        collaborators: ViewCollaborators | None = None,
    ) -> None:
        """Initialize the view engine with read-side domain view builders."""
        deps = collaborators if collaborators is not None else ViewCollaborators()
        self._config = config
        self._uow_factory = uow_factory
        self._filesystem_builder = (
            deps.filesystem_builder
            if deps.filesystem_builder is not None
            else FilesystemViewBuilder()
        )

    @staticmethod
    def _require_repo(uow: UnitOfWorkProtocol, repo: str | None) -> RepoRecord:
        """Resolve ``repo`` and return its ``RepoRecord``, raising ``DatabaseError`` if absent."""
        repo_id = uow.repos.resolve_id(repo)
        repo_record = uow.repos.get_by_id(repo_id)
        if repo_record is None:
            raise DatabaseError(
                f"Repository not found: {repo_id}",
                error_code="repo_not_found",
                context={"repo_id": repo_id},
            )
        return repo_record

    def get_filesystem_view(
        self,
        query: FilesystemViewQuery | None = None,
    ) -> FilesystemView:
        """Build an Explorer tree view of indexed folders and files for a repository.

        Args:
            query: Optional ``FilesystemViewQuery`` filtering repo, folder prefix,
                tests, and nesting depth.

        Returns:
            Constructed ``FilesystemView`` with subfolders ordered before files.

        Raises:
            DatabaseNotFoundError: If the database file does not exist.
            DatabaseError: If repository resolution fails.
            ValidationError: If a non-empty ``path_prefix`` matches no indexed folder.
        """
        effective = query if query is not None else FilesystemViewQuery()
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=True) as uow:
            repo_record = self._require_repo(uow, effective.repo)
            if effective.path_prefix is not None:
                normalized = relativize_if_under_root(
                    effective.path_prefix,
                    repo_record.root_path,
                )
                if normalized != effective.path_prefix:
                    effective = effective.model_copy(update={"path_prefix": normalized})
            return self._filesystem_builder.build_view(uow, repo_record, effective)


__all__ = [
    "ViewEngine",
]
