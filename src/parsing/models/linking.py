"""Intra-repository symbol binding, pass state, and link resolution report models."""

from pydantic import Field

from core.models.base import FrozenModel, MutableModel
from core.models.edge import Edge, EdgeTargetUpdate

UNRESOLVED_TARGET_PREFIXES: tuple[str, ...] = (
    "external_or_unresolved:",
    "unresolved_base:",
    "unresolved_call:",
)
RESOLVED_EDGE_CONFIDENCE: float = 0.9


class SymbolBinding(FrozenModel):
    """Per-file binding mapping a local or imported identifier to a resolved symbol ID."""

    file_id: str
    local_name: str
    symbol_id: str
    is_dotted_module: bool = False


class ResolutionState(MutableModel):
    """Mutable working state shared across import, inheritance, and call resolution passes."""

    file_id: str | None = None
    updates: list[EdgeTargetUpdate] = Field(default_factory=list)
    file_bindings: dict[str, dict[str, SymbolBinding]] = Field(default_factory=dict)
    dotted_import_map: dict[str, dict[str, str]] = Field(default_factory=dict)
    class_bases: dict[str, list[str]] = Field(default_factory=dict)
    edges_checked: int = 0
    imports_resolved: int = 0
    inherits_resolved: int = 0
    calls_resolved: int = 0
    edges_reset: int = 0

    def bind_symbol(
        self,
        file_id: str | None,
        local_name: str,
        symbol_id: str,
        *,
        is_dotted_module: bool = False,
    ) -> None:
        """Record a local symbol binding (and optional dotted module entry) for ``file_id``.

        Args:
            file_id: Evidence file identifier owning the import binding.
            local_name: Local alias or symbol name bound in ``file_id``.
            symbol_id: Resolved repository ``SymbolNode`` identifier.
            is_dotted_module: Whether to also register ``local_name`` in ``dotted_import_map``.
        """
        if not file_id or not local_name:
            return
        binding = SymbolBinding(
            file_id=file_id,
            local_name=local_name,
            symbol_id=symbol_id,
            is_dotted_module=is_dotted_module,
        )
        self.file_bindings.setdefault(file_id, {})[local_name] = binding
        if is_dotted_module:
            self.dotted_import_map.setdefault(file_id, {})[local_name] = symbol_id

    def record_dotted_module(self, file_id: str | None, module_name: str, module_id: str) -> None:
        """Register a direct dotted module import ``import pkg.mod`` for ``file_id``.

        Args:
            file_id: Evidence file identifier owning the import statement.
            module_name: Full dotted module name in the ``import`` clause.
            module_id: Resolved module ``SymbolNode`` identifier.
        """
        if not file_id or not module_name:
            return
        self.dotted_import_map.setdefault(file_id, {})[module_name] = module_id

    def get_binding_id(self, file_id: str | None, local_name: str) -> str | None:
        """Look up a bound symbol ID for ``local_name`` in ``file_id``.

        Args:
            file_id: File identifier to inspect.
            local_name: Bound local symbol or alias name.

        Returns:
            Resolved ``symbol_id`` if bound in ``file_id``, otherwise ``None``.
        """
        if not file_id:
            return None
        binding = self.file_bindings.get(file_id, {}).get(local_name)
        return binding.symbol_id if binding is not None else None

    def get_dotted_module_id(self, file_id: str | None, prefix: str) -> str | None:
        """Look up a dotted-import module ID for ``prefix`` in ``file_id``.

        Args:
            file_id: File identifier to inspect.
            prefix: Dotted module prefix or module alias.

        Returns:
            Resolved module ``symbol_id`` if registered, otherwise ``None``.
        """
        if not file_id:
            return None
        return self.dotted_import_map.get(file_id, {}).get(prefix)

    def add_class_base(self, class_id: str, base_class_id: str) -> None:
        """Append a resolved direct base class ID for ``class_id``.

        Args:
            class_id: Subclass ``SymbolNode`` identifier.
            base_class_id: Resolved base class ``SymbolNode`` identifier.
        """
        self.class_bases.setdefault(class_id, []).append(base_class_id)

    def stage_edge_update(
        self,
        edge: Edge,
        resolved_id: str | None,
        placeholder_id: str,
        default_confidence: float,
    ) -> tuple[int, int]:
        """Stage an ``EdgeTargetUpdate`` for a newly resolved or broken target edge.

        Args:
            edge: Candidate AST dependency edge being evaluated.
            resolved_id: Newly resolved target ``SymbolNode`` ID, or ``None`` if unresolved.
            placeholder_id: Canonical unresolved placeholder target ID for ``edge``.
            default_confidence: Default AST confidence restored when resetting broken links.

        Returns:
            Tuple ``(resolved_delta, reset_delta)`` indicating staged resolution or reset.
        """
        current_to = edge.to_id
        if resolved_id is not None:
            if current_to != resolved_id:
                self.updates.append(
                    EdgeTargetUpdate(
                        edge_id=edge.id,
                        to_id=resolved_id,
                        confidence=RESOLVED_EDGE_CONFIDENCE,
                    )
                )
                return 1, 0
            return 0, 0

        if not current_to.startswith(UNRESOLVED_TARGET_PREFIXES) and current_to != placeholder_id:
            self.updates.append(
                EdgeTargetUpdate(
                    edge_id=edge.id,
                    to_id=placeholder_id,
                    confidence=default_confidence,
                )
            )
            return 0, 1
        return 0, 0


class LinkResolutionReport(FrozenModel):
    """Summary metrics and staged ``EdgeTargetUpdate`` batch from ``SymbolLinker``."""

    repo_id: str
    file_id: str | None = None
    edges_checked: int = 0
    imports_resolved: int = 0
    inherits_resolved: int = 0
    calls_resolved: int = 0
    total_resolved: int = 0
    edges_reset: int = 0
    dangling_lsp_deleted: int = 0
    updates: list[EdgeTargetUpdate] = Field(default_factory=list)


__all__ = [
    "LinkResolutionReport",
    "RESOLVED_EDGE_CONFIDENCE",
    "ResolutionState",
    "SymbolBinding",
    "UNRESOLVED_TARGET_PREFIXES",
]
