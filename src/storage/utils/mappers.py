"""Bidirectional SQLAlchemy ``RowMapping`` <-> Pydantic ``FrozenModel`` mappers."""

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.engine import RowMapping

from core.models.edge import Edge
from core.models.enums import Language, SymbolKind, SymbolScope
from core.models.exceptions import ValidationError
from core.models.file import FileRecord
from core.models.node import SymbolNode, SymbolSummary
from core.models.range import Range
from core.models.repo import RepoRecord
from core.models.runs import IndexRunRecord, ReferenceRefreshState
from core.models.search import TextDocumentMeta, TextDocumentRecord
from core.utils.json_codec import from_json
from core.utils.logging import get_logger

logger = get_logger("storage.utils.mappers")


def _decode_range(raw: Any) -> Range | None:
    """Decode a stored JSON string or mapping into a ``Range`` model."""
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
    """Decode a stored JSON string or mapping into a ``dict[str, Any]``."""
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
    """Read an optional string column supporting ``_``-prefixed join aliases."""
    raw = row[alias_key] if alias_key in row else row.get(direct_key)
    return str(raw) if raw is not None else ""


def _pick_aliased_bool(
    row: RowMapping | Mapping[str, Any],
    alias_key: str,
    direct_key: str,
) -> bool:
    """Read an optional boolean column supporting ``_``-prefixed join aliases."""
    raw = row[alias_key] if alias_key in row else row.get(direct_key)
    return bool(raw) if raw is not None else False


def repo_to_row(repo: RepoRecord) -> dict[str, Any]:
    """Convert a ``RepoRecord`` entity into a SQLite column dictionary."""
    return {
        "id": repo.id,
        "root_path": repo.root_path,
        "name": repo.name,
        "default_language": str(repo.default_language),
        "created_at": repo.created_at,
        "last_indexed_at": repo.last_indexed_at,
    }


def row_to_repo(row: RowMapping | Mapping[str, Any]) -> RepoRecord:
    """Convert a database mapping row into a ``RepoRecord`` entity."""
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
    """Convert a ``FileRecord`` entity into a SQLite column dictionary."""
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
    """Convert a database mapping row into a ``FileRecord`` entity."""
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
    """Convert a ``SymbolNode`` entity into a SQLite column dictionary."""
    return {
        "id": node.id,
        "repo_id": node.repo_id,
        "file_id": node.file_id,
        "language": str(node.language),
        "kind": str(node.kind),
        "name": node.name,
        "qualified_name": node.qualified_name,
        "uri": node.uri,
        "range_json": node.decl_range,
        "selection_range_json": node.selection_range,
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
        "payload_json": node.payload,
        "scope": str(node.scope) if node.scope is not None else None,
        "lexical_parent_id": node.lexical_parent_id,
        "last_indexed_at": node.last_indexed_at,
    }


def _extract_common_node_fields(row: RowMapping | Mapping[str, Any]) -> dict[str, Any]:
    """Extract shared column values for ``SymbolNode`` and ``SymbolSummary`` mappers."""
    raw_scope = row.get("scope")
    return {
        "id": row["id"],
        "repo_id": row["repo_id"],
        "file_id": row["file_id"],
        "language": row.get("language") or Language.PYTHON,
        "kind": SymbolKind(row["kind"]),
        "name": row["name"],
        "qualified_name": row["qualified_name"],
        "uri": row["uri"],
        "content_hash": row.get("content_hash") or "",
        "confidence": float(row["confidence"]) if row.get("confidence") is not None else 1.0,
        "decl_range": _decode_range(row.get("range_json")),
        "selection_range": _decode_range(row.get("selection_range_json")),
        "start_line": row.get("start_line"),
        "end_line": row.get("end_line"),
        "signature": row.get("signature"),
        "parent_id": row.get("parent_id"),
        "visibility_hint": row.get("visibility_hint"),
        "doc_summary": row.get("doc_summary"),
        "scope": SymbolScope(raw_scope) if raw_scope is not None else None,
        "lexical_parent_id": row.get("lexical_parent_id"),
        "last_indexed_at": row.get("last_indexed_at"),
    }


def row_to_node(row: RowMapping | Mapping[str, Any]) -> SymbolNode:
    """Convert a database mapping row into a ``SymbolNode`` entity."""
    try:
        return SymbolNode(
            **_extract_common_node_fields(row),
            semantic_hash=row.get("semantic_hash") or "",
            source=row.get("source") or "python-ast",
            payload=_decode_payload(row.get("payload_json")),
        )
    except (PydanticValidationError, ValueError) as exc:
        logger.error("Failed mapping row to SymbolNode (id=%r): %s", row.get("id"), exc)
        raise ValidationError(
            f"Invalid SymbolNode row: {exc}",
            context={"node_id": row.get("id")},
        ) from exc


def row_to_symbol_summary(row: RowMapping | Mapping[str, Any]) -> SymbolSummary:
    """Convert a joined ``nodes`` + ``files`` row into a ``SymbolSummary`` projection."""
    try:
        return SymbolSummary(
            **_extract_common_node_fields(row),
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
    """Convert an ``Edge`` entity into a SQLite column dictionary."""
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
        "evidence_range_json": edge.evidence_range,
        "payload_json": edge.payload,
        "last_indexed_at": edge.last_indexed_at,
    }


def row_to_edge(row: RowMapping | Mapping[str, Any]) -> Edge:
    """Convert a database mapping row into an ``Edge`` entity."""
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


def row_to_run(row: RowMapping | Mapping[str, Any]) -> IndexRunRecord:
    """Convert a database mapping row into an ``IndexRunRecord`` entity."""
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
    """Convert a database mapping row into a ``ReferenceRefreshState`` entity."""
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


def text_doc_to_row(doc: TextDocumentRecord) -> dict[str, Any]:
    """Serialize a ``TextDocumentRecord`` into a ``text_files`` column dictionary."""
    return {
        "id": doc.id,
        "repo_id": doc.repo_id,
        "file_path": doc.file_path,
        "language": doc.language,
        "content_hash": doc.content_hash,
        "size_bytes": doc.size_bytes,
        "line_count": doc.line_count,
        "mtime_ns": doc.mtime_ns,
        "is_test": doc.is_test,
        "content": doc.content,
    }


def _extract_text_doc_meta_fields(row: RowMapping | Mapping[str, Any]) -> dict[str, Any]:
    """Extract common ``TextDocumentMeta`` fields from a ``text_files`` mapping row."""
    return {
        "id": str(row["id"]),
        "repo_id": str(row["repo_id"]),
        "file_path": str(row["file_path"]),
        "language": str(row["language"]),
        "content_hash": str(row["content_hash"]),
        "size_bytes": int(row["size_bytes"]),
        "line_count": int(row["line_count"]),
        "mtime_ns": int(row["mtime_ns"]),
        "is_test": bool(row["is_test"]),
    }


def row_to_text_doc_meta(row: RowMapping | Mapping[str, Any]) -> TextDocumentMeta:
    """Convert a ``text_files`` row into a ``TextDocumentMeta`` entity."""
    return TextDocumentMeta(**_extract_text_doc_meta_fields(row))


def row_to_text_doc_record(row: RowMapping | Mapping[str, Any]) -> TextDocumentRecord:
    """Convert a ``text_files`` row into a ``TextDocumentRecord`` entity."""
    return TextDocumentRecord(**_extract_text_doc_meta_fields(row), content=str(row["content"]))
