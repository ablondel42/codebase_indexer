"""SQLAlchemy Core table, index, and ``JsonText`` definitions for SQLite schema v4."""

import json
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.types import TypeDecorator

from core.models.exceptions import ValidationError
from core.utils.json_codec import to_json
from core.utils.logging import get_logger

logger = get_logger("storage.utils.schema")

SCHEMA_VERSION = 4


class JsonText(TypeDecorator[Any]):
    """SQLite ``TEXT`` column storing single-encoded JSON and returning decoded objects."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        """Serialize a Python value into single-encoded JSON text for SQLite binding.

        Args:
            value: Python value (``None``, pre-serialized JSON ``str``, or JSON-serializable).
            dialect: Active SQLAlchemy dialect (unused).

        Returns:
            Serialized JSON string or ``None`` when ``value`` is ``None``.
        """
        _ = dialect
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return to_json(value)

    def process_result_value(self, value: str | None, dialect: Any) -> Any:
        """Deserialize a SQLite JSON text value back into a Python object.

        Args:
            value: Raw ``TEXT`` column value from SQLite or ``None``.
            dialect: Active SQLAlchemy dialect (unused).

        Returns:
            Decoded Python object or ``None`` when ``value`` is ``None``.

        Raises:
            ValidationError: If ``value`` contains malformed JSON text.
        """
        _ = dialect
        if value is None:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            logger.error("Corrupted JSON column value at pos %d: %s", exc.pos, exc.msg)
            raise ValidationError(
                f"Corrupted JSON value in SQLite column: {exc}",
                context={"pos": exc.pos, "lineno": exc.lineno, "colno": exc.colno},
            ) from exc


metadata = MetaData()
_fts_metadata = MetaData()

repos_table = Table(
    "repos",
    metadata,
    Column("id", Text, primary_key=True),
    Column("root_path", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("default_language", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("last_indexed_at", Text, nullable=True),
)

files_table = Table(
    "files",
    metadata,
    Column("id", Text, primary_key=True),
    Column("repo_id", Text, ForeignKey("repos.id", ondelete="CASCADE"), nullable=False),
    Column("file_path", Text, nullable=False),
    Column("uri", Text, nullable=False),
    Column("module_path", Text, nullable=False),
    Column("language", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("line_count", Integer, nullable=False, default=0, server_default=text("0")),
    Column(
        "is_test",
        Boolean(create_constraint=False),
        nullable=False,
        default=False,
        server_default=text("0"),
    ),
    Column("last_modified_at", Text, nullable=False),
    Column("last_indexed_at", Text, nullable=True),
    UniqueConstraint("repo_id", "file_path"),
)

nodes_table = Table(
    "nodes",
    metadata,
    Column("id", Text, primary_key=True),
    Column("repo_id", Text, ForeignKey("repos.id", ondelete="CASCADE"), nullable=False),
    Column("file_id", Text, ForeignKey("files.id", ondelete="CASCADE"), nullable=False),
    Column("language", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("qualified_name", Text, nullable=False),
    Column("uri", Text, nullable=False),
    Column("range_json", JsonText, nullable=True),
    Column("selection_range_json", JsonText, nullable=True),
    Column("start_line", Integer, nullable=True),
    Column("end_line", Integer, nullable=True),
    Column("signature", Text, nullable=True),
    Column("parent_id", Text, nullable=True),
    Column("visibility_hint", Text, nullable=True),
    Column("doc_summary", Text, nullable=True),
    Column("content_hash", Text, nullable=False),
    Column("semantic_hash", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("payload_json", JsonText, nullable=False),
    Column("scope", Text, nullable=True),
    Column("lexical_parent_id", Text, nullable=True),
    Column("last_indexed_at", Text, nullable=True),
)

edges_table = Table(
    "edges",
    metadata,
    Column("id", Text, primary_key=True),
    Column("repo_id", Text, ForeignKey("repos.id", ondelete="CASCADE"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("from_id", Text, nullable=False),
    Column("to_id", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("evidence_file_id", Text, nullable=True),
    Column("evidence_uri", Text, nullable=True),
    Column("evidence_range_json", JsonText, nullable=True),
    Column("payload_json", JsonText, nullable=False),
    Column("last_indexed_at", Text, nullable=True),
)

index_runs_table = Table(
    "index_runs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("repo_id", Text, ForeignKey("repos.id", ondelete="CASCADE"), nullable=False),
    Column("run_type", Text, nullable=False),
    Column("started_at", Text, nullable=False),
    Column("completed_at", Text, nullable=True),
    Column("status", Text, nullable=False),
    Column("files_scanned", Integer, default=0, server_default=text("0")),
    Column("nodes_extracted", Integer, default=0, server_default=text("0")),
    Column("edges_extracted", Integer, default=0, server_default=text("0")),
    Column("error_message", Text, nullable=True),
)

reference_refresh_table = Table(
    "reference_refresh",
    metadata,
    Column(
        "target_symbol_id",
        Text,
        ForeignKey("nodes.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "available",
        Boolean(create_constraint=False),
        nullable=False,
        default=False,
        server_default=text("0"),
    ),
    Column("last_refreshed_at", Text, nullable=True),
    Column("refresh_status", Text, nullable=True),
    Column("error_code", Text, nullable=True),
)

text_files_table = Table(
    "text_files",
    metadata,
    Column("id", Text, primary_key=True),
    Column("repo_id", Text, ForeignKey("repos.id", ondelete="CASCADE"), nullable=False),
    Column("file_path", Text, nullable=False),
    Column("language", Text, nullable=False, default="text", server_default=text("'text'")),
    Column("content_hash", Text, nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("line_count", Integer, nullable=False, default=0, server_default=text("0")),
    Column("mtime_ns", Integer, nullable=False, default=0, server_default=text("0")),
    Column(
        "is_test",
        Boolean(create_constraint=False),
        nullable=False,
        default=False,
        server_default=text("0"),
    ),
    Column("content", Text, nullable=False),
    UniqueConstraint("repo_id", "file_path", name="uq_text_files_repo_path"),
)

Index("idx_files_repo_id", files_table.c.repo_id)
Index("idx_files_uri", files_table.c.uri)

Index("idx_nodes_repo_id", nodes_table.c.repo_id)
Index("idx_nodes_file_id", nodes_table.c.file_id)
Index("idx_nodes_qualified_name", nodes_table.c.qualified_name)
Index("idx_nodes_name", nodes_table.c.name)
Index(
    "idx_nodes_repo_kind_qname",
    nodes_table.c.repo_id,
    nodes_table.c.kind,
    nodes_table.c.qualified_name,
)
Index("idx_nodes_parent_id", nodes_table.c.parent_id)
Index("idx_nodes_lexical_parent_id", nodes_table.c.lexical_parent_id)
Index("idx_nodes_file_line", nodes_table.c.file_id, nodes_table.c.start_line)

Index("idx_edges_repo_id", edges_table.c.repo_id)
Index("idx_edges_from_kind", edges_table.c.from_id, edges_table.c.kind)
Index("idx_edges_to_kind", edges_table.c.to_id, edges_table.c.kind)
Index("idx_edges_evfile_kind", edges_table.c.evidence_file_id, edges_table.c.kind)

Index("idx_index_runs_repo_id", index_runs_table.c.repo_id)
Index("idx_index_runs_status", index_runs_table.c.status)
Index("idx_reference_refresh_available", reference_refresh_table.c.available)
Index("idx_text_files_repo_id", text_files_table.c.repo_id)
Index("idx_text_files_repo_lang", text_files_table.c.repo_id, text_files_table.c.language)

# Unmanaged FTS5 virtual table descriptors for SQLAlchemy Core SELECT/JOIN queries
nodes_fts_table = Table(
    "nodes_fts",
    _fts_metadata,
    Column("rowid", Integer),
    Column("node_id", Text),
    Column("repo_id", Text),
    Column("name", Text),
    Column("qualified_name", Text),
    Column("signature", Text),
    Column("doc_summary", Text),
)

text_fts_table = Table(
    "text_fts",
    _fts_metadata,
    Column("rowid", Integer),
    Column("repo_id", Text),
    Column("file_path", Text),
    Column("content", Text),
)
