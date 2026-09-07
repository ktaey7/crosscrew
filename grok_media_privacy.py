#!/usr/bin/env python3
"""Inspect Grok Build privacy and private video-output readiness without secrets."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - host fallback
    tomllib = None


ZDR_BLOCKED_REASONS = {
    "BLOCKED_REASON_NO_LOGS",
    "BLOCKED_REASON_NO_LOGS_MODERATED",
}

# Field names we read out of Grok's auth record to decide whether video output
# needs a private destination. These belong to xAI, not to us: if a rename lands,
# the fields simply vanish and every account starts looking unrestricted. That
# would silently re-open the preflight and burn a source frame on a request the
# API is going to reject, so their *presence* is checked separately from their
# value and an unrecognized record is treated as restricted.
SCOPE_SCHEMA_FIELDS = ("coding_data_retention_opt_out", "team_blocked_reasons")

# Scopes that require a caller-owned upload destination for video.
RESTRICTED_SCOPES = {"team_zdr", "coding_data_opt_out", "schema_unrecognized", "unknown"}


def grok_home() -> Path:
    override = os.environ.get("GROK_HOME")
    return Path(override).expanduser() if override else Path.home() / ".grok"


def _read_toml(path: Path) -> dict[str, Any]:
    if tomllib is None or not path.is_file():
        return {}
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _auth_entries(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    return [entry for entry in payload.values() if isinstance(entry, dict)]


def scope_detail(home: Path | None = None) -> dict[str, Any]:
    """Resolve the privacy scope and say whether the auth record was recognized.

    A record we cannot interpret is reported as ``schema_unrecognized`` rather
    than being flattened into ``standard``, because "we could not tell" and
    "there is no restriction" must not lead to the same decision.
    """
    root = home or grok_home()
    entries = _auth_entries(root / "auth.json")
    if not entries:
        return {
            "scope": "unknown",
            "schema_recognized": False,
            "schema_note": "auth.json을 읽을 수 없음 (미로그인 또는 파싱 실패)",
        }
    recognized = any(field in entry for entry in entries for field in SCOPE_SCHEMA_FIELDS)
    if not recognized:
        return {
            "scope": "schema_unrecognized",
            "schema_recognized": False,
            "schema_note": (
                "auth.json에 알려진 프라이버시 필드가 없음 "
                f"({', '.join(SCOPE_SCHEMA_FIELDS)}) — xAI 스키마 변경 가능성"
            ),
        }
    blocked = {
        reason
        for entry in entries
        for reason in (entry.get("team_blocked_reasons") or [])
        if isinstance(reason, str)
    }
    if blocked & ZDR_BLOCKED_REASONS:
        return {"scope": "team_zdr", "schema_recognized": True, "schema_note": None}
    if any(entry.get("coding_data_retention_opt_out") is True for entry in entries):
        return {"scope": "coding_data_opt_out", "schema_recognized": True, "schema_note": None}
    return {"scope": "standard", "schema_recognized": True, "schema_note": None}


def privacy_scope(home: Path | None = None) -> str:
    """Return team_zdr, coding_data_opt_out, standard, schema_unrecognized, or unknown."""
    return scope_detail(home)["scope"]


def _private_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_mode & 0o077 == 0
    except OSError:
        return False


def private_video_output(home: Path | None = None) -> dict[str, Any]:
    """Report whether effective Grok config has a safe, complete S3 output target."""
    root = home or grok_home()
    sources = [
        Path("/etc/grok/managed_config.toml"),
        root / "managed_config.toml",
        root / "config.toml",
    ]
    effective: dict[str, Any] = {}
    contributing: list[Path] = []
    for source in sources:
        layer = _read_toml(source)
        if layer:
            effective = _deep_merge(effective, layer)
            contributing.append(source)

    tools = effective.get("tools") if isinstance(effective.get("tools"), dict) else {}
    s3 = tools.get("zdr_video_output_s3") if isinstance(tools.get("zdr_video_output_s3"), dict) else {}
    credentials = s3.get("read_write") if isinstance(s3.get("read_write"), dict) else {}
    endpoint = str(s3.get("endpoint") or "").strip()
    required = {
        "bucket": str(s3.get("bucket") or "").strip(),
        "endpoint": endpoint,
        "region": str(s3.get("region") or "").strip(),
        "access_key_id": str(credentials.get("access_key_id") or "").strip(),
        "secret_access_key": str(credentials.get("secret_access_key") or "").strip(),
    }
    secret_source = next(
        (
            source
            for source in reversed(contributing)
            if isinstance(_read_toml(source).get("tools"), dict)
            and isinstance(_read_toml(source)["tools"].get("zdr_video_output_s3"), dict)
        ),
        None,
    )
    complete = (
        tools.get("disable_zdr_incompatible_tools") is True
        and all(required.values())
        and urlparse(endpoint).scheme == "https"
    )
    private = bool(secret_source and _private_file(secret_source))
    return {
        "configured": complete and private,
        "complete": complete,
        "private_permissions": private,
        "config_source": str(secret_source) if secret_source else None,
    }


def video_privacy_preflight(home: Path | None = None) -> dict[str, Any]:
    detail = scope_detail(home)
    scope = detail["scope"]
    output = private_video_output(home)
    # Fail closed: anything other than a recognized, unrestricted account requires
    # a private destination. Guessing "unrestricted" from an unreadable record
    # would let the request through and waste a paid source frame on an API call
    # that rejects it.
    required = scope in RESTRICTED_SCOPES
    report = {
        "privacy_scope": scope,
        "schema_recognized": detail["schema_recognized"],
        "private_output_required": required,
        "private_output_configured": output["configured"],
        "private_output_complete": output["complete"],
        "private_output_permissions_ok": output["private_permissions"],
        "config_source": output["config_source"],
        "ready": not required or output["configured"],
    }
    if not detail["schema_recognized"]:
        report["schema_drift"] = detail["schema_note"]
        report["fail_closed"] = True
    return report
