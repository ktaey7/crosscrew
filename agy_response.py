"""Parse terminal AGY responses without exposing diagnostic metadata."""
from __future__ import annotations

import json
import uuid
import provider_usage

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "thinking_tokens",
    "cache_read_tokens",
    "total_tokens",
)


def _usage_record(payload) -> dict | None:
    if not isinstance(payload, dict):
        return None
    tokens = {}
    for key in TOKEN_FIELDS:
        value = payload.get(key)
        tokens[key] = value if type(value) is int and 0 <= value <= 2**63 - 1 else None
    if all(value is None for value in tokens.values()):
        return None
    return provider_usage.usage_record("agy", tokens, "agy.json.usage")


def parse_agy_response(raw: str) -> dict:
    """Return allowlisted text, status, and observed session/usage metadata."""
    result = {
        "status": "invalid_output",
        "stdout": "",
        "session_id": None,
        "usage": None,
    }
    if not raw.strip():
        result["status"] = "empty_output"
        return result
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        if not raw.lstrip().startswith(("{", "[")):
            result.update(status="ok", stdout=raw)
        return result
    if not isinstance(payload, dict):
        return result
    status = payload.get("status")
    response = payload.get("response")
    if not isinstance(status, str) or not isinstance(response, str):
        return result

    conversation_id = payload.get("conversation_id")
    if isinstance(conversation_id, str):
        try:
            result["session_id"] = str(uuid.UUID(conversation_id))
        except ValueError:
            pass
    result["usage"] = _usage_record(payload.get("usage"))

    denied_actions = payload.get("denied_actions")
    if isinstance(denied_actions, list) and denied_actions:
        result["status"] = "tool_permission_denied"
    elif status != "SUCCESS":
        result["status"] = "error"
    elif not response.strip():
        result["status"] = "empty_output"
    else:
        result.update(status="ok", stdout=response)
    return result
