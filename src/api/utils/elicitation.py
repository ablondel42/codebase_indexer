"""Stateless ACP v1 elicitation mode gating, schema validation, and cancellation helpers."""

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from api.models.acp import ClientCapabilities
from api.models.client_ops import (
    CreateElicitationParams,
    CreateElicitationResponse,
    ElicitationSchema,
    PendingElicitationRequest,
)
from api.models.config import CapabilityPolicyConfig
from api.utils.errors import pydantic_error_to_acp_error
from api.utils.security import sanitize_meta_dict, validate_elicitation_url
from core.models.exceptions import AcpInvalidParamsError


def check_elicitation_mode_supported(
    mode: str,
    ws_client_caps: ClientCapabilities,
    policy: CapabilityPolicyConfig,
) -> None:
    """Verify the client explicitly advertised ``mode`` and policy permits it (else ``-32602``)."""
    elic_caps = ws_client_caps.elicitation
    if elic_caps is None:
        raise AcpInvalidParamsError(
            f"Client did not advertise elicitation support for mode {mode!r}"
        )
    if mode == "form":
        if elic_caps.form is None or not policy.allow_elicitation_form:
            raise AcpInvalidParamsError("Client did not advertise elicitation.form capability")
        return
    if mode == "url":
        if elic_caps.url is None or not policy.allow_elicitation_url:
            raise AcpInvalidParamsError("Client did not advertise elicitation.url capability")
        return
    raise AcpInvalidParamsError(f"Unsupported elicitation mode: {mode!r}")


def validate_elicitation_request(
    req_id: int | str,
    raw_params: object,
    ws_client_caps: ClientCapabilities,
    policy: CapabilityPolicyConfig,
    active_url_ids: set[str] | frozenset[str],
) -> PendingElicitationRequest:
    """Validate an agent-to-client ``elicitation/create`` call against ACP spec & policy."""
    if not isinstance(raw_params, Mapping):
        raise AcpInvalidParamsError("Invalid elicitation/create params: expected JSON object")
    try:
        parsed = CreateElicitationParams.model_validate(raw_params)
    except PydanticValidationError as exc:
        raise pydantic_error_to_acp_error(exc, label="elicitation/create") from exc

    check_elicitation_mode_supported(parsed.mode, ws_client_caps, policy)
    if parsed.mode == "url" and parsed.elicitation_id is not None:
        if parsed.elicitation_id in active_url_ids:
            raise AcpInvalidParamsError(
                f"Duplicate active URL elicitationId: '{parsed.elicitation_id}'"
            )
        if parsed.url is not None:
            validate_elicitation_url(parsed.url, allow_http=policy.elicitation_allow_http_url)
    return PendingElicitationRequest(
        request_id=req_id,
        mode=parsed.mode,
        session_id=parsed.session_id,
        elicitation_id=parsed.elicitation_id,
        requested_schema=parsed.requested_schema,
    )


def _check_string_constraints(prop_name: str, prop_def: Mapping[str, Any], value: object) -> None:
    """Validate string primitive, length bounds, and enum constraints."""
    if not isinstance(value, str):
        raise AcpInvalidParamsError(f"Elicitation field '{prop_name}' must be a string")
    min_len = prop_def.get("minLength")
    max_len = prop_def.get("maxLength")
    if isinstance(min_len, int) and len(value) < min_len:
        raise AcpInvalidParamsError(f"Elicitation field '{prop_name}' is shorter than minLength")
    if isinstance(max_len, int) and len(value) > max_len:
        raise AcpInvalidParamsError(f"Elicitation field '{prop_name}' exceeds maxLength")
    enum_vals = prop_def.get("enum")
    if isinstance(enum_vals, list) and value not in enum_vals:
        raise AcpInvalidParamsError(f"Elicitation field '{prop_name}' is not an allowed enum value")


def _check_number_constraints(
    prop_name: str,
    prop_def: Mapping[str, Any],
    value: object,
    *,
    integer_only: bool,
) -> None:
    """Validate integer/number type and inclusive min/max bounds."""
    if isinstance(value, bool):
        raise AcpInvalidParamsError(f"Elicitation field '{prop_name}' cannot be a boolean")
    num_val: int | float
    if integer_only:
        if not isinstance(value, int):
            raise AcpInvalidParamsError(f"Elicitation field '{prop_name}' must be an integer")
        num_val = value
    else:
        if not isinstance(value, (int, float)):
            raise AcpInvalidParamsError(f"Elicitation field '{prop_name}' must be a number")
        num_val = value
    minimum = prop_def.get("minimum")
    maximum = prop_def.get("maximum")
    if isinstance(minimum, (int, float)) and num_val < minimum:
        raise AcpInvalidParamsError(f"Elicitation field '{prop_name}' is below minimum")
    if isinstance(maximum, (int, float)) and num_val > maximum:
        raise AcpInvalidParamsError(f"Elicitation field '{prop_name}' exceeds maximum")


def _check_array_constraints(prop_name: str, prop_def: Mapping[str, Any], value: object) -> None:
    """Validate multi-select string array item count constraints."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AcpInvalidParamsError(f"Elicitation field '{prop_name}' must be an array of strings")
    min_items = prop_def.get("minItems")
    max_items = prop_def.get("maxItems")
    if isinstance(min_items, int) and len(value) < min_items:
        raise AcpInvalidParamsError(f"Elicitation field '{prop_name}' has too few items")
    if isinstance(max_items, int) and len(value) > max_items:
        raise AcpInvalidParamsError(f"Elicitation field '{prop_name}' has too many items")


def _validate_single_property(field_name: str, prop_def: Mapping[str, Any], val: object) -> None:
    """Validate one submitted form field against its declared property schema."""
    prop_type = prop_def.get("type")
    if prop_type == "string":
        _check_string_constraints(field_name, prop_def, val)
    elif prop_type == "integer":
        _check_number_constraints(field_name, prop_def, val, integer_only=True)
    elif prop_type == "number":
        _check_number_constraints(field_name, prop_def, val, integer_only=False)
    elif prop_type == "boolean" and not isinstance(val, bool):
        raise AcpInvalidParamsError(f"Elicitation field '{field_name}' must be a boolean")
    elif prop_type == "array":
        _check_array_constraints(field_name, prop_def, val)


def _validate_form_content(
    content: Mapping[str, Any],
    schema: ElicitationSchema,
) -> dict[str, Any]:
    """Validate submitted form ``content`` against ``schema.required`` and ``properties``."""
    for req_field in schema.required or ():
        if req_field not in content:
            raise AcpInvalidParamsError(f"Missing required elicitation field '{req_field}'")
    for field_name, val in content.items():
        prop_def = schema.properties.get(field_name)
        if isinstance(prop_def, Mapping):
            _validate_single_property(field_name, prop_def, val)
    return dict(content)


def _validate_elicitation_error_response(
    raw_err: object,
    request_id: int | str,
) -> dict[str, Any]:
    """Validate and copy a JSON-RPC error object returned for ``elicitation/create``."""
    if not isinstance(raw_err, Mapping):
        raise AcpInvalidParamsError("Invalid JSON-RPC error object in elicitation response")
    code = raw_err.get("code")
    message = raw_err.get("message")
    if isinstance(code, bool) or not isinstance(code, int) or not isinstance(message, str):
        raise AcpInvalidParamsError("Invalid JSON-RPC error code or message")
    out_err: dict[str, Any] = {"code": code, "message": message}
    if "data" in raw_err and raw_err["data"] is not None:
        out_err["data"] = raw_err["data"]
    return {"jsonrpc": "2.0", "id": request_id, "error": out_err}


def validate_elicitation_response(
    msg: Mapping[str, Any],
    pending: PendingElicitationRequest,
) -> dict[str, Any]:
    """Validate and scrub a client JSON-RPC response answering ``elicitation/create``."""
    if "error" in msg:
        return _validate_elicitation_error_response(msg.get("error"), pending.request_id)

    raw_result = msg.get("result")
    if not isinstance(raw_result, Mapping):
        raise AcpInvalidParamsError("Invalid elicitation result: expected JSON object")
    try:
        parsed = CreateElicitationResponse.model_validate(raw_result)
    except PydanticValidationError as exc:
        raise pydantic_error_to_acp_error(exc, label="elicitation response") from exc

    result_payload: dict[str, Any] = {"action": parsed.action}
    if parsed.action == "accept" and parsed.content is not None:
        result_payload["content"] = (
            _validate_form_content(parsed.content, pending.requested_schema)
            if pending.mode == "form" and pending.requested_schema is not None
            else dict(parsed.content)
        )
    scrubbed_meta = sanitize_meta_dict(parsed.meta)
    if scrubbed_meta is not None:
        result_payload["_meta"] = scrubbed_meta
    return {"jsonrpc": "2.0", "id": pending.request_id, "result": result_payload}


def build_cancelled_elicitation_response(request_id: int | str) -> dict[str, Any]:
    """Build an ACP ``elicitation/create`` response with ``action='cancel'``."""
    return {"jsonrpc": "2.0", "id": request_id, "result": {"action": "cancel"}}


__all__ = [
    "build_cancelled_elicitation_response",
    "check_elicitation_mode_supported",
    "validate_elicitation_request",
    "validate_elicitation_response",
]
