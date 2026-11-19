"""Bidirectional SQLAlchemy ``RowMapping`` <-> Pydantic ``FrozenModel`` mappers."""

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.engine import RowMapping

from core.models.edge import Edge, ReferencedByDetail
from core.models.enums import Language, SymbolKind, SymbolScope
from core.models.exceptions import ValidationError
from core.models.file import FileRecord
from core.models.node import SymbolNode, SymbolSummary
from core.models.range import Range
from core.models.repo import RepoRecord
from core.models.runs import IndexRunRecord, ReferenceRefreshState
from core.utils.json_codec import from_json, to_json
from core.utils.logging import get_logger

logger = get_logger("storage.utils.mappers")


def _decode_range(raw: Any) -> Range | None:
    """Decode a stored JSON string or mapping into a ``Range`` model.

    Args:
        raw: ``None``, ``Range``, JSON ``str``, or decoded ``Mapping``.

    Returns:
        Validated ``Range`` instance or ``None`` when ``raw`` is ``None``.

    Raises:
        ValidationError: If ``raw`` cannot be decoded into a valid ``Range``.
    """
    if raw is None:
        return None
    if isinstance(raw, Range):
        return raw
    decoded = from_json(raw) if isinstance(raw, str) else raw
    try:
        return Range.model_validate(decoded)
    except PydanticValidationError as exc:
        logger.error("Failed validating stored Range payload: %s", exc)
        raise ValidationError(f"Invalid stored Range payload: {exc}") from exc


def _decode_payload(raw: Any) -> dict[str, Any]:
    """Decode a stored JSON string or mapping into a ``dict[str, Any]``.

    Args:
        raw: ``None``, JSON ``str``, or decoded ``Mapping``.

    Returns:
        Python dictionary decoded from ``raw`` (empty dict when ``raw`` is ``None``).

    Raises:
        ValidationError: If ``raw`` decodes to a non-mapping JSON value.
    """
    if raw is None:
        return {}
    decoded = from_json(raw) if isinstance(raw, str) else raw
    if not isinstance(decoded, Mapping):
        logger.error("Expected JSON object payload, got %s", type(decoded).__name__)
        raise ValidationError(
            f"Expected JSON object payload, got {type(decoded).__name__}",
            context={"actual_type": type(decoded).__name__},
        )
    return dict(decoded)


def _pick_aliased_str(
    row: RowMapping | Mapping[str, Any],
    alias_key: str,
    direct_key: str,
) -> str:
    """Read an optional string column supporting ``_``-prefixed join aliases.

    Args:
        row: Database mapping row.
        alias_key: Prefixed alias column name (e.g., ``"_file_path"``).
        direct_key: Unprefixed column name (e.g., ``"file_path"``).

    Returns:
        String value or ``""`` when absent or ``None``.
    """
    raw = row[alias_key] if alias_key in row else row.get(direct_key)
    return str(raw) if raw is not None else ""


def _pick_aliased_bool(
    row: RowMapping | Mapping[str, Any],
    alias_key: str,
    direct_key: str,
) -> bool:
    """Read an optional boolean column supporting ``_``-prefixed join aliases.

    Args:
        row: Database mapping row.
        alias_key: Prefixed alias column name (e.g., ``"_is_test"``).
        direct_key: Unprefixed column name (e.g., ``"is_test"``).

    Returns:
        Boolean value or ``False`` when absent or ``None``.
    """
    raw = row[alias_key] if alias_key in row else row.get(direct_key)
    return bool(raw) if raw is not None else False


def repo_to_row(repo: RepoRecord) -> dict[str, Any]:
    """Convert a ``RepoRecord`` entity into a SQLite column dictionary.

    Args:
        repo: Repository entity to serialize.

    Returns:
        Column-value mapping matching ``repos_table``.
    """
    return {
        "id": repo.id,
        "root_path": repo.root_path,
        "name": repo.name,
        "default_language": str(repo.default_language),
        "created_at": repo.created_at,
        "last_indexed_at": repo.last_indexed_at,
    }


def row_to_repo(row: RowMapping | Mapping[str, Any]) -> RepoRecord:
    """Convert a database mapping row into a ``RepoRecord`` entity.

    Args:
        row: SQLite row from ``repos_table``.

    Returns:
        Validated ``RepoRecord`` instance.

    Raises:
        ValidationError: If row fields violate ``RepoRecord`` invariants.
    """
    try:
        return RepoRecord(
            id=row["id"],
            root_path=row["root_path"],
            name=row["name"],
            default_language=Language(row["default_language"]),
            created_at=row["created_at"],
            last_indexed_at=row.get("last_indexed_at"),
        )
    except (PydanticValidationError, ValueError) as exc:
        logger.error("Failed mapping row to RepoRecord (id=%r): %s", row.get("id"), exc)
        raise ValidationError(
            f"Invalid RepoRecord row: {exc}",
            context={"repo_id": row.get("id")},
        ) from exc


def file_to_row(file_record: FileRecord) -> dict[str, Any]:
    """Convert a ``FileRecord`` entity into a SQLite column dictionary.

    Args:
        file_record: File entity to serialize.

    Returns:
        Column-value mapping matching ``files_table``.
    """
    return {
        "id": file_record.id,
        "repo_id": file_record.repo_id,
        "file_path": file_record.file_path,
        "uri": file_record.uri,
        "module_path": file_record.module_path,
        "language": str(file_record.language),
        "content_hash": file_record.content_hash,
        "size_bytes": file_record.size_bytes,
        "line_count": file_record.line_count,
        "is_test": bool(file_record.is_test),
        "last_modified_at": file_record.last_modified_at,
        "last_indexed_at": file_record.last_indexed_at,
    }


def row_to_file(row: RowMapping | Mapping[str, Any]) -> FileRecord:
    """Convert a database mapping row into a ``FileRecord`` entity.

    Args:
        row: SQLite row from ``files_table``.

    Returns:
        Validated ``FileRecord`` instance.

    Raises:
        ValidationError: If row fields violate ``FileRecord`` invariants.
    """
    try:
        return FileRecord(
            id=row["id"],
            repo_id=row["repo_id"],
            file_path=row["file_path"],
            uri=row["uri"],
            module_path=row["module_path"],
            language=row["language"],
            content_hash=row["content_hash"],
            size_bytes=int(row["size_bytes"]),
            line_count=int(row["line_count"] or 0),
            is_test=bool(row["is_test"]),
            last_modified_at=row["last_modified_at"],
            last_indexed_at=row.get("last_indexed_at"),
        )
    except (PydanticValidationError, ValueError) as exc:
        logger.error("Failed mapping row to FileRecord (id=%r): %s", row.get("id"), exc)
        raise ValidationError(
            f"Invalid FileRecord row: {exc}",
            context={"file_id": row.get("id")},
        ) from exc


def node_to_row(node: SymbolNode) -> dict[str, Any]:
    """Convert a ``SymbolNode`` entity into a SQLite column dictionary.

    Serializes ``decl_range -> range_json``, ``selection_range -> selection_range_json``,
    and ``payload -> payload_json`` as single-encoded JSON strings compatible with ``JsonText``.

    Args:
        node: Symbol node entity to serialize.

    Returns:
        Column-value mapping matching ``nodes_table``.
    """
    return {
        "id": node.id,
        "repo_id": node.repo_id,
        "file_id": node.file_id,
        "language": str(node.language),
        "kind": str(node.kind),
        "name": node.name,
        "qualified_name": node.qualified_name,
        "uri": node.uri,
        "range_json": to_json(node.decl_range) if node.decl_range is not None else None,
        "selection_range_json": (
            to_json(node.selection_range) if node.selection_range is not None else None
        ),
        "start_line": node.start_line,
        "end_line": node.end_line,
        "signature": node.signature,
        "parent_id": node.parent_id,
        "visibility_hint": node.visibility_hint,
        "doc_summary": node.doc_summary,
        "content_hash": node.content_hash,
        "semantic_hash": node.semantic_hash,
        "source": node.source,
        "confidence": node.confidence,
        "payload_json": to_json(node.payload),
        "scope": str(node.scope) if node.scope is not None else None,
        "lexical_parent_id": node.lexical_parent_id,
        "last_indexed_at": node.last_indexed_at,
    }


def row_to_node(row: RowMapping | Mapping[str, Any]) -> SymbolNode:
    """Convert a database mapping row into a ``SymbolNode`` entity.

    Args:
        row: SQLite row from ``nodes_table``.

    Returns:
        Validated ``SymbolNode`` instance with decoded ``Range`` and ``payload`` fields.

    Raises:
        ValidationError: If row fields or JSON columns violate ``SymbolNode`` invariants.
    """
    raw_scope = row.get("scope")
    try:
        return SymbolNode(
            id=row["id"],
            repo_id=row["repo_id"],
            file_id=row["file_id"],
            language=row.get("language") or Language.PYTHON,
            kind=SymbolKind(row["kind"]),
            name=row["name"],
            qualified_name=row["qualified_name"],
            uri=row["uri"],
            content_hash=row.get("content_hash") or "",
            semantic_hash=row.get("semantic_hash") or "",
            source=row.get("source") or "python-ast",
            confidence=float(row["confidence"]) if row.get("confidence") is not None else 1.0,
            payload=_decode_payload(row.get("payload_json")),
            decl_range=_decode_range(row.get("range_json")),
            selection_range=_decode_range(row.get("selection_range_json")),
            start_line=row.get("start_line"),
            end_line=row.get("end_line"),
            signature=row.get("signature"),
            parent_id=row.get("parent_id"),
            visibility_hint=row.get("visibility_hint"),
            doc_summary=row.get("doc_summary"),
            scope=SymbolScope(raw_scope) if raw_scope is not None else None,
            lexical_parent_id=row.get("lexical_parent_id"),
            last_indexed_at=row.get("last_indexed_at"),
        )
    except (PydanticValidationError, ValueError) as exc:
        logger.error("Failed mapping row to SymbolNode (id=%r): %s", row.get("id"), exc)
        raise ValidationError(
            f"Invalid SymbolNode row: {exc}",
            context={"node_id": row.get("id")},
        ) from exc


def row_to_symbol_summary(row: RowMapping | Mapping[str, Any]) -> SymbolSummary:
    """Convert a joined ``nodes`` + ``files`` row into a ``SymbolSummary`` projection.

    Reads ``_file_path`` / ``file_path``, ``_module_path`` / ``module_path``, and
    ``_is_test`` / ``is_test`` aliases seamlessly.

    Args:
        row: SQLite mapping row from ``nodes_table`` joined with ``files_table``.

    Returns:
        Validated ``SymbolSummary`` projection.

    Raises:
        ValidationError: If row fields violate ``SymbolSummary`` invariants.
    """
    raw_scope = row.get("scope")
    try:
        return SymbolSummary(
            id=row["id"],
            repo_id=row["repo_id"],
            file_id=row["file_id"],
            kind=SymbolKind(row["kind"]),
            name=row["name"],
            qualified_name=row["qualified_name"],
            uri=row["uri"],
            language=row.get("language") or Language.PYTHON,
            signature=row.get("signature"),
            doc_summary=row.get("doc_summary"),
            visibility_hint=row.get("visibility_hint"),
            scope=SymbolScope(raw_scope) if raw_scope is not None else None,
            parent_id=row.get("parent_id"),
            lexical_parent_id=row.get("lexical_parent_id"),
            start_line=row.get("start_line"),
            end_line=row.get("end_line"),
            decl_range=_decode_range(row.get("range_json")),
            selection_range=_decode_range(row.get("selection_range_json")),
            confidence=float(row["confidence"]) if row.get("confidence") is not None else 1.0,
            content_hash=row.get("content_hash") or "",
            last_indexed_at=row.get("last_indexed_at"),
            file_path=_pick_aliased_str(row, "_file_path", "file_path"),
            module_path=_pick_aliased_str(row, "_module_path", "module_path"),
            is_test=_pick_aliased_bool(row, "_is_test", "is_test"),
        )
    except (PydanticValidationError, ValueError) as exc:
        logger.error("Failed mapping row to SymbolSummary (id=%r): %s", row.get("id"), exc)
        raise ValidationError(
            f"Invalid SymbolSummary row: {exc}",
            context={"node_id": row.get("id")},
        ) from exc


def edge_to_row(edge: Edge) -> dict[str, Any]:
    """Convert an ``Edge`` entity into a SQLite column dictionary.

    Serializes ``evidence_range -> evidence_range_json`` and ``payload -> payload_json``
    as single-encoded JSON strings compatible with ``JsonText``.

    Args:
        edge: Graph edge entity to serialize.

    Returns:
        Column-value mapping matching ``edges_table``.
    """
    return {
        "id": edge.id,
        "repo_id": edge.repo_id,
        "kind": str(edge.kind),
        "from_id": edge.from_id,
        "to_id": edge.to_id,
        "source": edge.source,
        "confidence": edge.confidence,
        "evidence_file_id": edge.evidence_file_id,
        "evidence_uri": edge.evidence_uri,
        "evidence_range_json": (
            to_json(edge.evidence_range) if edge.evidence_range is not None else None
        ),
        "payload_json": to_json(edge.payload),
        "last_indexed_at": edge.last_indexed_at,
    }


def row_to_edge(row: RowMapping | Mapping[str, Any]) -> Edge:
    """Convert a database mapping row into an ``Edge`` entity.

    Args:
        row: SQLite row from ``edges_table``.

    Returns:
        Validated ``Edge`` instance with decoded ``evidence_range`` and ``payload``.

    Raises:
        ValidationError: If row fields or JSON columns violate ``Edge`` invariants.
    """
    try:
        return Edge(
            id=row["id"],
            repo_id=row["repo_id"],
            kind=row["kind"],
            from_id=row["from_id"],
            to_id=row["to_id"],
            source=row.get("source") or "python-ast",
            confidence=float(row["confidence"]) if row.get("confidence") is not None else 1.0,
            payload=_decode_payload(row.get("payload_json")),
            evidence_file_id=row.get("evidence_file_id"),
            evidence_uri=row.get("evidence_uri"),
            evidence_range=_decode_range(row.get("evidence_range_json")),
            last_indexed_at=row.get("last_indexed_at"),
        )
    except (PydanticValidationError, ValueError) as exc:
        logger.error("Failed mapping row to Edge (id=%r): %s", row.get("id"), exc)
        raise ValidationError(
            f"Invalid Edge row: {exc}",
            context={"edge_id": row.get("id")},
        ) from exc


def row_to_referenced_by(row: RowMapping | Mapping[str, Any]) -> ReferencedByDetail:
    """Convert a joined referencing-symbol row into a ``ReferencedByDetail`` model.

    Args:
        row: SQLite row joining ``edges_table``, ``nodes_table``, and ``files_table``.

    Returns:
        Validated ``ReferencedByDetail`` instance.

    Raises:
        ValidationError: If row fields violate ``ReferencedByDetail`` invariants.
    """
    raw_scope = row.get("scope")
    try:
        return ReferencedByDetail(
            id=row["id"],
            repo_id=row["repo_id"],
            file_id=row["file_id"],
            language=row.get("language") or Language.PYTHON,
            kind=SymbolKind(row["kind"]),
            name=row["name"],
            qualified_name=row["qualified_name"],
            uri=row["uri"],
            start_line=row.get("start_line"),
            end_line=row.get("end_line"),
            signature=row.get("signature"),
            scope=SymbolScope(raw_scope) if raw_scope is not None else None,
            lexical_parent_id=row.get("lexical_parent_id"),
            file_path=row.get("file_path"),
            module_path=row.get("module_path"),
        )
    except (PydanticValidationError, ValueError) as exc:
        logger.error("Failed mapping row to ReferencedByDetail (id=%r): %s", row.get("id"), exc)
        raise ValidationError(
            f"Invalid ReferencedByDetail row: {exc}",
            context={"node_id": row.get("id")},
        ) from exc


def row_to_run(row: RowMapping | Mapping[str, Any]) -> IndexRunRecord:
    """Convert a database mapping row into an ``IndexRunRecord`` entity.

    Args:
        row: SQLite row from ``index_runs_table``.

    Returns:
        Validated ``IndexRunRecord`` instance.

    Raises:
        ValidationError: If row fields violate ``IndexRunRecord`` invariants.
    """
    try:
        return IndexRunRecord(
            id=row["id"],
            repo_id=row["repo_id"],
            run_type=row["run_type"],
            started_at=row["started_at"],
            completed_at=row.get("completed_at"),
            status=row["status"],
            files_scanned=int(row.get("files_scanned") or 0),
            nodes_extracted=int(row.get("nodes_extracted") or 0),
            edges_extracted=int(row.get("edges_extracted") or 0),
            error_message=row.get("error_message"),
        )
    except (PydanticValidationError, ValueError) as exc:
        logger.error("Failed mapping row to IndexRunRecord (id=%r): %s", row.get("id"), exc)
        raise ValidationError(
            f"Invalid IndexRunRecord row: {exc}",
            context={"run_id": row.get("id")},
        ) from exc


def row_to_refresh_state(row: RowMapping | Mapping[str, Any]) -> ReferenceRefreshState:
    """Convert a database mapping row into a ``ReferenceRefreshState`` entity.

    Args:
        row: SQLite row from ``reference_refresh_table``.

    Returns:
        Validated ``ReferenceRefreshState`` instance.

    Raises:
        ValidationError: If row fields violate ``ReferenceRefreshState`` invariants.
    """
    try:
        return ReferenceRefreshState(
            target_symbol_id=row["target_symbol_id"],
            available=bool(row["available"]),
            last_refreshed_at=row.get("last_refreshed_at"),
            refresh_status=row.get("refresh_status"),
            error_code=row.get("error_code"),
        )
    except (PydanticValidationError, ValueError) as exc:
        logger.error(
            "Failed mapping row to ReferenceRefreshState (symbol=%r): %s",
            row.get("target_symbol_id"),
            exc,
        )
        raise ValidationError(
            f"Invalid ReferenceRefreshState row: {exc}",
            context={"target_symbol_id": row.get("target_symbol_id")},
        ) from exc
