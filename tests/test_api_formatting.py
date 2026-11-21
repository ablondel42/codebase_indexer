"""Unit tests for ACP terminal log multiline and unified-diff formatting."""

import json

from api.utils.formatting import format_raw_frame_for_log, indent_json_payload


def test_indent_json_payload_renders_real_newlines_and_redacts_secrets() -> None:
    """Multiline strings render as ``|`` blocks while sensitive keys remain redacted."""
    payload = {
        "apiKey": "secret-123",
        "args": {
            "Path": "/workspace/app.tsx",
            "Replacement": "const a = 1;\nconst b = 2;",
        },
    }
    rendered = indent_json_payload(payload, indent_spaces=2)

    assert "[REDACTED]" in rendered
    assert "secret-123" not in rendered
    assert '"Replacement": |\n' in rendered
    assert "const a = 1;\n" in rendered
    assert "const b = 2;" in rendered
    assert "\\n" not in rendered


def test_format_raw_frame_collapses_diff_blocks_into_unified_diff() -> None:
    """ACP ``type: diff`` blocks replace full ``oldText``/``newText`` with ``unifiedDiff``."""
    frame = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "content": [
                        {
                            "type": "diff",
                            "path": "/repo/src/session-chat.tsx",
                            "oldText": "line1\nremove_me\nline3\n",
                            "newText": "line1\nline3\n",
                        }
                    ],
                }
            },
        }
    )

    label, pretty = format_raw_frame_for_log(frame)

    assert label == "notification · session/update"
    assert '"oldText"' not in pretty
    assert '"newText"' not in pretty
    assert '"unifiedDiff": |\n' in pretty
    assert "--- a/session-chat.tsx" in pretty
    assert "+++ b/session-chat.tsx" in pretty
    assert "-remove_me" in pretty
