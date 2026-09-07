#!/usr/bin/env python3
"""mission 이벤트 append-only 로그.

이 로그는 위임 계층의 관측물이지 실행 경로가 아니다. 그래서 쓰기 함수는 **절대
예외를 밖으로 내보내지 않는다**. 실패하면 False를 돌려주고, 호출자는 envelope에
degraded 표시만 남긴 뒤 위임을 계속한다.

mission_id는 치환하지 않고 거부한다. run_id와 달리 신규 식별자라서 처음부터
엄격하게 갈 수 있다 (run_registry.py는 기존 파일 호환 때문에 충돌 탐지 방식).
charset이 경로 구분자와 선행 점을 배제하므로 mission_id는 경로 탈출에 쓸 수 없다.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re

SCHEMA_VERSION = 1
MISSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ROLE_MAX = 32
ROUND_MAX = 99


def valid_mission_id(value) -> bool:
    return isinstance(value, str) and bool(MISSION_ID.match(value))


def valid_role(value) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= ROLE_MAX:
        return False
    return not any(ch < " " or ch == "\x7f" for ch in value)


def valid_round(value) -> bool:
    """bool은 int의 서브클래스다. True가 라운드 1로 통과하면 안 된다."""
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= ROUND_MAX


def mission_dir(state_dir: Path, mission_id: str) -> Path:
    return state_dir / "missions" / mission_id


def append(state_dir: Path, mission_id: str, event: dict) -> bool:
    """이벤트 한 줄을 덧붙인다. 성공하면 True. 절대 raise 하지 않는다."""
    if not valid_mission_id(mission_id) or not isinstance(event, dict):
        return False
    # 호출자가 v/t를 덮어쓰지 못하게 event를 먼저 깔고 그 위에 덮는다.
    record = dict(event)
    record["v"] = SCHEMA_VERSION
    record["t"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        directory = mission_dir(state_dir, mission_id)
        # mkdir(parents=True)는 중간 디렉터리에 mode를 적용하지 않고 umask를 따른다.
        # missions/ 자체도 0700이어야 하므로 부모를 따로 만들고 chmod한다.
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.parent.chmod(0o700)
        directory.mkdir(exist_ok=True)
        directory.chmod(0o700)
        path = directory / "events.jsonl"
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            written = os.write(handle, line)     # O_APPEND 한 줄 쓰기는 원자적이다
        finally:
            os.close(handle)
        if written != len(line):
            # 부분 쓰기를 성공으로 보고하면 로그에 없는 이벤트를 있다고 믿게 된다.
            return False
        os.chmod(path, 0o600)
        return True
    except (OSError, ValueError, TypeError):
        return False


def read_events(state_dir: Path, mission_id: str) -> list[dict]:
    if not valid_mission_id(mission_id):
        return []
    try:
        text = (mission_dir(state_dir, mission_id) / "events.jsonl").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError는 OSError가 아니다. 읽기가 예외를 밖으로 내보내면
        # 관측이 실행보다 시끄러워진다 (스펙 §4.1).
        return []
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue           # 부분 쓰기는 정상 상황이다. 그 줄만 건너뛴다.
        if isinstance(payload, dict):
            events.append(payload)
    return events
