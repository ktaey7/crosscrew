#!/usr/bin/env python3
"""Typed request contract for the multi-AI escalation broker.

The broker runs outside every host sandbox, so its request surface is the real
privilege boundary. It therefore never accepts an argv from a client. Clients
send a typed JSON request; this module validates every field against the same
enumerations the CLI exposes and *constructs* the argv itself. A field that does
not validate is rejected before any process is spawned.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys

import effort_registry
import media_registry
import mission_log
import work_commands

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CROSSCREW_BACKENDS_CONFIG", ROOT / "backends.json"))
_REGISTRY_CACHE: dict | None = None


def _registry() -> dict:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        try:
            _REGISTRY_CACHE = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _REGISTRY_CACHE = {}
    return _REGISTRY_CACHE

PROVIDERS = ("claude", "codex", "grok", "agy")
HOSTS = ("claude", "codex", "grok", "agy", "shell")
PROFILES = ("review", "work", "research", "media")
MODES = ("oneshot", "fresh", "resume")
JOB_ACTIONS = ("start", "status", "collect", "cancel", "purge")
CALL_ACTIONS = ("check", "call")
ACTIONS = JOB_ACTIONS + CALL_ACTIONS
ASPECT_RATIOS = (
    "auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3",
    "2:1", "1:2", "19.5:9", "9:19.5", "20:9", "9:20",
)
DURATIONS = (6, 10)

# broker가 **메모리에 들고 있는** 계약면. 이 파일들이 바뀌면 돌고 있는 데몬은
# 낡은 것이다. 재시작 없이는 새 필드를 조용히 버린다 (2026-07-27 실측).
#
# worker_job.py·call_worker.py는 여기 없다. broker가 subprocess로 띄우므로 항상
# 디스크의 최신 코드이고, 넣으면 무관한 편집마다 오탐이 난다.
CONTRACT_FILES = (
    "work_commands.py",
    "multiai_broker.py",     # 서버 자신
    "broker_protocol.py",    # 검증과 argv 생성
    "effort_registry.py",    # validate가 부르는 판정기
    "media_registry.py",
    "mission_log.py",
    "backends.json",         # _REGISTRY_CACHE에 캐시된다 — 두 번째 staleness 벡터
)


def contract_hash() -> str:
    """계약면 파일들의 내용 해시. 돌고 있는 broker와 디스크를 비교하는 데 쓴다."""
    digest = hashlib.sha256()
    for name in CONTRACT_FILES:
        digest.update(name.encode("utf-8"))
        try:
            digest.update((CONFIG_PATH if name == "backends.json" else ROOT / name).read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()[:16]


ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9_.:\[\]-]{1,64}$")
MAX_CHECK_AFTER = 86400.0
MAX_PURGE_DAYS = 3650


class RequestError(ValueError):
    """A client request failed validation and must not be executed."""

    def __init__(self, field: str, detail: str) -> None:
        super().__init__(f"{field}: {detail}")
        self.field = field
        self.detail = detail


def _enum(payload: dict, field: str, allowed: tuple, required: bool = False):
    value = payload.get(field)
    if value is None:
        if required:
            raise RequestError(field, "required")
        return None
    if value not in allowed:
        raise RequestError(field, f"must be one of {list(allowed)}")
    return value


def _identifier(payload: dict, field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise RequestError(field, "must match [A-Za-z0-9_.-]{1,128}")
    return value


def _model(payload: dict) -> str | None:
    value = payload.get("model")
    if value is None:
        return None
    if not isinstance(value, str) or not MODEL_PATTERN.match(value):
        raise RequestError("model", "must match [A-Za-z0-9_.:[]-]{1,64}")
    return value


def _effort(payload: dict, provider: str) -> str | None:
    value = payload.get("effort")
    if value is None:
        return None
    if not isinstance(value, str):
        raise RequestError("effort", "must be a string")
    error = effort_registry.validation_error(_registry(), provider, value)
    if error:
        _status, hint = error
        raise RequestError("effort", hint)
    return value


def _mission(payload: dict) -> str | None:
    value = payload.get("mission")
    if value is None:
        return None
    if not mission_log.valid_mission_id(value):
        raise RequestError("mission", "must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
    return value


def _role(payload: dict) -> str | None:
    value = payload.get("role")
    if value is None:
        return None
    if not mission_log.valid_role(value):
        raise RequestError(
            "role", f"must be 1..{mission_log.ROLE_MAX} chars without control characters"
        )
    return value


def _round(payload: dict) -> int | None:
    value = payload.get("round")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RequestError("round", "must be an integer")
    if not 1 <= value <= mission_log.ROUND_MAX:
        raise RequestError("round", f"must be between 1 and {mission_log.ROUND_MAX}")
    return value


def _path(payload: dict, field: str, kind: str, required: bool = False) -> Path | None:
    value = payload.get(field)
    if value is None:
        if required:
            raise RequestError(field, "required")
        return None
    if not isinstance(value, str) or not value:
        raise RequestError(field, "must be a non-empty string")
    path = Path(value)
    if not path.is_absolute():
        raise RequestError(field, "must be an absolute path")
    if ".." in path.parts:
        raise RequestError(field, "must not contain '..'")
    resolved = path.resolve()
    if kind == "file" and not resolved.is_file():
        raise RequestError(field, "file not found")
    if kind == "dir" and not resolved.is_dir():
        raise RequestError(field, "directory not found")
    return resolved


def _number(payload: dict, field: str, maximum: float) -> float | None:
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError(field, "must be a number")
    if value < 0 or value > maximum:
        raise RequestError(field, f"must be between 0 and {maximum}")
    return float(value)


def validate(payload: dict) -> dict:
    """Return a normalized request, or raise RequestError."""
    if not isinstance(payload, dict):
        raise RequestError("body", "must be a JSON object")
    action = _enum(payload, "action", ACTIONS, required=True)
    request: dict = {"action": action}

    if action in ("status", "collect", "cancel"):
        job_id = _identifier(payload, "job_id")
        if not job_id:
            raise RequestError("job_id", "required")
        request["job_id"] = job_id
        return request

    if action == "purge":
        days = _number(payload, "older_than_days", MAX_PURGE_DAYS)
        request["older_than_days"] = days if days is not None else 30.0
        return request

    request["provider"] = _enum(payload, "provider", PROVIDERS, required=True)
    request["host"] = _enum(payload, "host", HOSTS, required=True)
    request["profile"] = _enum(payload, "profile", PROFILES) or "review"
    try:
        request["allow_commands"] = work_commands.validate(
            payload.get("allow_commands"), request["provider"], request["profile"])
    except ValueError as error:
        raise RequestError("allow_commands", str(error)) from error

    if action == "check":
        if request["allow_commands"]:
            raise RequestError("allow_commands", "command grants require a work call or start, not check")
        return request

    request["mode"] = _enum(payload, "mode", MODES) or "oneshot"
    request["brief"] = _path(payload, "brief", "file", required=True)
    request["target"] = _path(payload, "target", "dir", required=True)
    request["run_id"] = _identifier(payload, "run_id")
    request["job_id"] = _identifier(payload, "job_id")
    request["session_id"] = _identifier(payload, "session_id")
    request["model"] = _model(payload)
    # The registry decides which provider accepts a reasoning effort and which
    # values are legal, so the broker rejects on the same grounds the dispatcher
    # would rather than forwarding a value the provider will choke on.
    request["effort"] = _effort(payload, request["provider"])
    # Codex host -> Claude/Grok is a `broker` route, so labels that do not cross
    # this hop vanish exactly where a sandboxed host needs them most.
    request["mission"] = _mission(payload)
    request["role"] = _role(payload)
    request["round"] = _round(payload)
    if action != "start" and any(
        request[field] is not None for field in ("mission", "role", "round")
    ):
        # Only `worker_job.py start` records a job.json; `call_worker.py` has no
        # --mission flag and no place to keep one. Accepting the label here would
        # either build an argv the dispatcher rejects or drop it in silence.
        raise RequestError("mission", "mission labels require action=start")
    request["media_kind"] = _enum(payload, "media_kind", ("image", "video"))
    request["aspect_ratio"] = _enum(payload, "aspect_ratio", ASPECT_RATIOS)
    request["duration"] = _enum(payload, "duration", DURATIONS)
    request["output_dir"] = _path(payload, "output_dir", "dir")
    request["rehydrate_file"] = _path(payload, "rehydrate_file", "file")
    request["check_after"] = _number(payload, "check_after", MAX_CHECK_AFTER)

    if request["profile"] == "media":
        if request["mode"] != "fresh":
            raise RequestError("mode", "media requires fresh")
        if not request["media_kind"] or not request["output_dir"]:
            raise RequestError("media_kind", "media requires media_kind and output_dir")
        # Capability comes from the registry so the broker, the supervisor, and
        # the dispatcher cannot drift apart on who can generate what.
        media_error = media_registry.validation_error(
            _registry(),
            request["provider"],
            request["media_kind"],
            request["aspect_ratio"],
            request["duration"],
        )
        if media_error:
            _status, hint = media_error
            raise RequestError("media_kind", hint)
    elif any(
        request[field] is not None
        for field in ("media_kind", "aspect_ratio", "duration", "output_dir")
    ):
        raise RequestError("profile", "media options require profile=media")

    return request


def build_argv(request: dict) -> list[str]:
    """Construct the adapter argv for a validated request.

    Every element is either a fixed literal or a value that `validate` already
    constrained to an enumeration, an identifier charset, or an existing path.
    """
    action = request["action"]

    if action in ("status", "collect", "cancel"):
        return [sys.executable, str(ROOT / "worker_job.py"), action, request["job_id"]]

    if action == "purge":
        return [
            sys.executable,
            str(ROOT / "worker_job.py"),
            "purge",
            "--older-than-days",
            str(int(request["older_than_days"])),
        ]

    if action == "check":
        return [
            sys.executable,
            str(ROOT / "call_worker.py"),
            request["provider"],
            "--host",
            request["host"],
            "--profile",
            request["profile"],
            "--check",
        ]

    script = "worker_job.py" if action == "start" else "call_worker.py"
    argv = [sys.executable, str(ROOT / script)]
    if action == "start":
        argv.append("start")
    argv += [
        request["provider"],
        str(request["brief"]),
        "--host",
        request["host"],
        "--target",
        str(request["target"]),
        "--profile",
        request["profile"],
        "--mode",
        request["mode"],
    ]
    if request.get("run_id"):
        argv += ["--run-id", request["run_id"]]
    if request.get("session_id"):
        argv += ["--session-id", request["session_id"]]
    if request.get("model"):
        argv += ["--model", request["model"]]
    if request.get("effort"):
        argv += ["--effort", request["effort"]]
    for command in request.get("allow_commands", []):
        argv += ["--allow-command", command]
    if request.get("media_kind"):
        argv += ["--media-kind", request["media_kind"]]
    if request.get("output_dir"):
        argv += ["--output-dir", str(request["output_dir"])]
    if request.get("aspect_ratio"):
        argv += ["--aspect-ratio", request["aspect_ratio"]]
    if request.get("duration"):
        argv += ["--duration", str(request["duration"])]
    if request.get("rehydrate_file"):
        argv += ["--rehydrate-file", str(request["rehydrate_file"])]
    if action == "start":
        if request.get("job_id"):
            argv += ["--job-id", request["job_id"]]
        if request.get("check_after") is not None:
            argv += ["--check-after", str(request["check_after"])]
        # Only the supervisor accepts these; `validate` already refuses them for
        # any other action, and the gate here keeps that true if that changes.
        if request.get("mission"):
            argv += ["--mission", request["mission"]]
        if request.get("role"):
            argv += ["--role", request["role"]]
        if request.get("round"):
            argv += ["--round", str(request["round"])]
    else:
        argv += ["--timeout", "0"]
    # The broker *is* the escalated context. Marking the call keeps the adapter
    # from re-emitting needs_host_escalation for the host that delegated here.
    argv.append("--host-escalated")
    return argv
