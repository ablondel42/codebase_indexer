"""Pure signature and annotation introspection helpers for MCP tool schemas."""

import inspect
from typing import Any

from pydantic import AliasChoices
from pydantic.fields import FieldInfo

from mcp_server.models.inputs import BaseMcpToolInput

_PK = inspect.Parameter.POSITIONAL_OR_KEYWORD


def _wire_param_name(field_name: str, field_info: FieldInfo) -> str:
    """Resolve the public MCP wire parameter name for a Pydantic model field."""
    val_alias = field_info.validation_alias
    if isinstance(val_alias, AliasChoices) and val_alias.choices:
        return str(val_alias.choices[0])
    if isinstance(val_alias, str) and val_alias:
        return val_alias
    return field_name


def build_mcp_tool_signature(
    input_cls: type[BaseMcpToolInput],
) -> tuple[inspect.Signature, dict[str, Any]]:
    """Derive ``(inspect.Signature, __annotations__)`` from ``input_cls`` omitting ``db_path``."""
    required_params: list[inspect.Parameter] = []
    optional_params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}

    for field_name, field_info in input_cls.model_fields.items():
        if field_name == "db_path":
            continue
        param_name = _wire_param_name(field_name, field_info)
        ann = field_info.annotation if field_info.annotation is not None else Any
        annotations[param_name] = ann
        if field_info.is_required():
            required_params.append(inspect.Parameter(param_name, _PK, annotation=ann))
        else:
            optional_params.append(
                inspect.Parameter(param_name, _PK, default=field_info.default, annotation=ann)
            )

    annotations["return"] = str
    signature = inspect.Signature(
        parameters=[*required_params, *optional_params],
        return_annotation=str,
    )
    return signature, annotations


__all__ = [
    "build_mcp_tool_signature",
]
