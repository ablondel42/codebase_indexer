"""Filesystem Explorer tree view builder over indexed repository text metadata."""

from core.models.exceptions import ValidationError
from core.models.repo import RepoRecord
from core.ports.uow import UnitOfWorkProtocol
from core.utils.logging import get_logger
from core.utils.pathing import normalize_rel_path_prefix
from views.models.filesystem import FilesystemView, FilesystemViewQuery
from views.utils.tree import build_folder_tree

logger = get_logger("views.services.filesystem")


class FilesystemViewBuilder:
    """Constructs Explorer-ordered ``FilesystemView`` trees from ``text_files`` metadata."""

    @staticmethod
    def build_view(
        uow: UnitOfWorkProtocol,
        repo: RepoRecord,
        query: FilesystemViewQuery,
    ) -> FilesystemView:
        """Build an Explorer tree of folders and files for ``repo``.

        Args:
            uow: Active read Unit of Work.
            repo: Target ``RepoRecord`` metadata.
            query: Filter and depth parameters for the filesystem view.

        Returns:
            Constructed ``FilesystemView`` with subfolders ordered before files.

        Raises:
            ValidationError: If a non-empty ``path_prefix`` matches no indexed folder.
        """
        focus_prefix = normalize_rel_path_prefix(query.path_prefix)
        all_docs = uow.text_search.list_metadata_for_repo(repo.id)
        prefix_slash = f"{focus_prefix}/" if focus_prefix else ""
        filtered = [
            doc
            for doc in all_docs
            if (query.include_tests or not doc.is_test)
            and (not prefix_slash or doc.file_path.startswith(prefix_slash))
        ]
        if focus_prefix and not filtered:
            raise ValidationError(
                f"Folder path not found in indexed repository: {focus_prefix}",
                error_code="view_path_not_found",
                context={"repo_id": repo.id, "path_prefix": focus_prefix},
            )
        root = build_folder_tree(
            repo.name,
            filtered,
            focus_prefix=focus_prefix,
            max_depth=query.max_depth,
        )
        logger.debug(
            "Built FilesystemView for %s (focus=%s, matched_files=%d, max_depth=%s)",
            repo.id,
            focus_prefix or ".",
            len(filtered),
            query.max_depth,
        )
        return FilesystemView(repo_id=repo.id, repo_name=repo.name, root=root)


__all__ = [
    "FilesystemViewBuilder",
]
