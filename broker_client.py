#!/usr/bin/env python3
"""Client side of the loopback escalation broker.

Used by ``call_worker.py`` and ``worker_job.py`` when the host sandbox cannot
spawn a provider itself. Every failure path here is non-fatal: if the broker is
absent, stale, or unreachable, the caller falls back to the historical
``needs_host_escalation`` envelope so behaviour without a broker is unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.request

import broker_endpoint as endpoint
import broker_protocol as protocol

HEALTH_TIMEOUT = 2.0
ACTION_TIMEOUT = 180.0
CALL_TIMEOUT = 3900.0


class BrokerUnavailable(RuntimeError):
    """The broker could not be reached; the caller must fall back."""


def in_broker_child() -> bool:
    """True when this process was itself spawned by the broker.

    Prevents a sandbox failure inside a brokered worker from asking the broker
    to escalate again.
    """
    return os.environ.get("CROSSCREW_BROKER_CHILD") == "1"


class BrokerStale(BrokerUnavailable):
    """돌고 있는 broker가 디스크의 계약과 다른 코드를 들고 있다.

    launchd 데몬은 기동 시점의 broker_protocol과 backends.json을 메모리에
    스냅샷한다. 계약을 바꾸고 재시작하지 않으면 새 필드를 조용히 버린다
    (2026-07-27 실측: mission 라벨이 오류 없이 사라졌다). 조용히 잘못 실행되느니
    보내지 않는다. BrokerUnavailable을 상속하므로 기존 호출부는 그대로
    needs_host_escalation으로 fail-closed한다.
    """


def _resolve(state_dir: Path) -> tuple[str, str]:
    if in_broker_child():
        raise BrokerUnavailable("already running as a broker child")
    if os.environ.get("CROSSCREW_BROKER_DISABLE") == "1":
        raise BrokerUnavailable("broker disabled by CROSSCREW_BROKER_DISABLE")
    info = endpoint.read_endpoint(state_dir)
    if not info:
        raise BrokerUnavailable("no broker endpoint registered")
    token = endpoint.read_token(state_dir)
    if not token:
        raise BrokerUnavailable("no readable broker token")
    current = protocol.contract_hash()
    if info.get("contract_hash") != current:
        raise BrokerStale(
            "the running broker was started with a different contract "
            f"(advertised {info.get('contract_hash')!r}, on disk {current!r}). "
            "It would silently drop fields it does not know. "
            "Run: broker_service.sh restart"
        )
    return f"http://127.0.0.1:{info['port']}", token


def _post(base: str, token: str, payload: dict, timeout: float) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/request",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Multiai-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise BrokerUnavailable(f"broker HTTP {exc.code}") from exc
        if isinstance(parsed, dict) and parsed.get("status"):
            # A validation rejection is a real answer, not an outage.
            return parsed
        raise BrokerUnavailable(f"broker HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise BrokerUnavailable(str(exc)) from exc


def unavailable_reason(state_dir: Path) -> str | None:
    """broker에 왜 못 보내는지. 보낼 수 있으면 None.

    health()는 None만 돌려주므로 --check가 "broker_available: false"만 보이고
    이유가 사라진다. 사용자는 브로커가 죽은 줄 알고 health를 쳐서 정상이라는
    답을 받는다. 낡은 것과 죽은 것은 조치가 다르므로 구분해서 알려준다.
    """
    try:
        _resolve(state_dir)
    except BrokerUnavailable as exc:
        return str(exc)
    return None


def health(state_dir: Path) -> dict | None:
    """Return the broker health payload, or None when unavailable."""
    try:
        base, _token = _resolve(state_dir)
    except BrokerUnavailable:
        return None
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=HEALTH_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def available(state_dir: Path) -> bool:
    return health(state_dir) is not None


def request(state_dir: Path, payload: dict) -> dict:
    """Send a typed request through the broker.

    Raises BrokerUnavailable when the broker cannot be reached at all.
    """
    base, token = _resolve(state_dir)
    timeout = CALL_TIMEOUT if payload.get("action") == "call" else ACTION_TIMEOUT
    return _post(base, token, payload, timeout)
