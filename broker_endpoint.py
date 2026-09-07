#!/usr/bin/env python3
"""Shared endpoint/token discovery for the multi-AI escalation broker.

The broker exists so a sandboxed host (Codex, Grok, agy) can start a worker
that must run outside that sandbox. Both the server and its clients resolve the
loopback endpoint and shared secret through this module so there is exactly one
definition of where those live and how strict their permissions must be.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parent
TOKEN_BYTES = 32


def broker_dir(state_dir: Path) -> Path:
    return state_dir / "broker"


def endpoint_path(state_dir: Path) -> Path:
    return broker_dir(state_dir) / "endpoint.json"


def token_path(state_dir: Path) -> Path:
    return broker_dir(state_dir) / "token"


def request_log_path(state_dir: Path) -> Path:
    return broker_dir(state_dir) / "requests.jsonl"


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    path.chmod(0o600)


def read_token(state_dir: Path) -> str | None:
    path = token_path(state_dir)
    try:
        if path.stat().st_mode & 0o077:
            # A token other accounts can read is not a shared secret. Refuse it
            # rather than silently authenticating against a weakened file.
            return None
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def issue_token(state_dir: Path) -> str:
    ensure_private_dir(broker_dir(state_dir))
    token = secrets.token_urlsafe(TOKEN_BYTES)
    write_private(token_path(state_dir), token + "\n")
    return token


def read_endpoint(state_dir: Path) -> dict | None:
    try:
        data = json.loads(endpoint_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("port"), int):
        return None
    if not isinstance(data.get("contract_hash"), str):
        # 이 필드를 모르는 옛 broker가 광고한 endpoint다. 그 프로세스는 지금
        # 계약을 모른다는 뜻이므로 낡은 것으로 취급한다.
        return None
    if data.get("host") != "127.0.0.1":
        # The broker is a local privilege boundary. A non-loopback advertisement
        # is either stale or tampered with; both must fail closed.
        return None
    return data


def write_endpoint(state_dir: Path, port: int, pid: int, started_at: str,
                   contract_hash: str) -> None:
    ensure_private_dir(broker_dir(state_dir))
    write_private(
        endpoint_path(state_dir),
        json.dumps(
            {
                "schema_version": "1.0",
                "host": "127.0.0.1",
                "port": port,
                "pid": pid,
                "started_at": started_at,
                # 이 프로세스가 기동할 때의 계약면 해시. 클라이언트가 디스크와
                # 대조해 낡은 데몬에게 보내지 않는다.
                "contract_hash": contract_hash,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )


def clear_endpoint(state_dir: Path) -> None:
    try:
        endpoint_path(state_dir).unlink()
    except OSError:
        pass
