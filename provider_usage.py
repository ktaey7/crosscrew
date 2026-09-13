"""Small, allowlisted terminal usage records; never infer missing token counts."""
from __future__ import annotations

import json
import uuid
import datetime as dt
import hashlib
import os
from pathlib import Path
import stat
from urllib.parse import quote

FIELDS = {
    "claude": ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"),
    "codex": ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"),
    "grok": ("input_tokens", "cached_input_tokens", "cache_creation_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens", "model_calls"),
    "agy": ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens"),
}
SOURCES = {
    "claude": {"claude.result.usage"},
    "codex": {"codex.turn.completed.usage", "codex.turn.failed.usage"},
    "grok": {"grok.session.usage.json"},
    "agy": {"agy.json.usage"},
}
AGGREGATION = {"claude": "invocation", "codex": "session_cumulative", "grok": "session_cumulative", "agy": "unverified"}


def usage_record(provider: str, payload, source: str) -> dict | None:
    if not isinstance(payload, dict):
        return None
    values = {key: value if type(value) is int and 0 <= value <= 2**63 - 1 else None
              for key in FIELDS[provider] for value in [payload.get(key)]}
    if all(value is None for value in values.values()):
        return None
    return {"source": source, "scope": "provider_terminal", "aggregation": AGGREGATION[provider], "tokens": values}


def validated_usage(provider, value) -> dict | None:
    if not isinstance(provider, str) or provider not in FIELDS or not isinstance(value, dict):
        return None
    source = value.get("source")
    if (value.get("scope") != "provider_terminal" or not isinstance(source, str)
            or source not in SOURCES[provider]
            or value.get("aggregation", AGGREGATION[provider]) != AGGREGATION[provider]):
        return None
    return usage_record(provider, value.get("tokens"), source)


def grok_usage(target: Path, session_id: str | None, started_at: str) -> dict | None:
    """Read only this exact session's fresh native counter file; no discovery."""
    fds = []
    try:
        if not session_id or str(uuid.UUID(session_id)) != session_id:
            return None
        base = Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok")))
        fds.append(os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        parts = ("sessions", quote(str(target.resolve()), safe=""), session_id)
        for part in parts:
            fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
        fd = os.open("usage.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fds[-1])
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 2 * 1024 * 1024:
                return None
            raw = stream.read(2 * 1024 * 1024 + 1)
            after = os.fstat(stream.fileno())
            if len(raw) != before.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                return None
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("sessionId") != session_id:
            return None
        if dt.datetime.fromisoformat(data["updatedAt"]) < dt.datetime.fromisoformat(started_at):
            return None
        native = data.get("session")
        if not isinstance(native, dict):
            return None
        mapping = dict(input_tokens="inputTokens", cached_input_tokens="cachedReadTokens",
                       cache_creation_input_tokens="cacheCreationTokens", output_tokens="outputTokens",
                       reasoning_output_tokens="reasoningTokens", total_tokens="totalTokens", model_calls="modelCalls")
        usage = usage_record("grok", {key: native.get(field) for key, field in mapping.items()}, "grok.session.usage.json")
        if usage:
            usage["source_ref"] = {"path": str(base.joinpath(*parts, "usage.json")), "size_bytes": len(raw),
                                   "sha256": hashlib.sha256(raw).hexdigest()}
        return usage
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def claude_result(raw: str) -> tuple[str, str | None, dict | None, bool, str | None]:
    """Final text, observed session, usage, provider error, format error.

    Plain text remains compatible with older CLI wrappers. Broken JSON is not
    promoted to a successful answer and its raw metadata is never echoed.
    """
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        if raw.lstrip().startswith(("{", "[")):
            return "", None, None, False, "invalid_json"
        return raw, None, None, False, None
    if not isinstance(value, dict) or value.get("type") != "result":
        return "", None, None, False, "invalid_result"
    sid = None
    try:
        sid = str(uuid.UUID(value["session_id"])) if isinstance(value.get("session_id"), str) else None
    except ValueError:
        pass
    text = value.get("result")
    error = value.get("is_error") is True or str(value.get("subtype", "")).startswith("error_")
    # Some error results have an errors[] list instead of result. Do not dump
    # arbitrary diagnostic payloads into stdout just to make it nonempty.
    if not isinstance(text, str):
        text = "Provider reported an execution error." if error else ""
    usage = usage_record("claude", value.get("usage"), "claude.result.usage")
    return text, sid, usage, error, None


def codex_usage(raw: str) -> dict | None:
    latest = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            continue
        if event["type"] not in {"turn.completed", "turn.failed"}:
            continue
        # A terminal record is already aggregated by the provider. Keep the
        # latest record; summing repeated terminal events would double count.
        latest = usage_record("codex", event.get("usage"), f"codex.{event['type']}.usage")
    return latest
