#!/usr/bin/env python3
"""run registry 경로·읽기·소유권. dispatcher와 supervisor가 공유한다.

run ID는 파일명으로 쓰기 위해 치환되므로 `a/b`와 `a b`가 같은 파일이 된다.
그 파일의 session ID를 조용히 공유하면 서로 다른 논리적 작업이 같은 provider
대화에 올라탄다. 소유권을 write lock 안에서 확정해 fail-closed 한다.

경로 계산이 두 파일에 복제돼 있으면 한쪽만 고쳐도 눈치채지 못하므로 여기 둔다.
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path
import re

SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


class RunIdCollision(Exception):
    """서로 다른 run ID가 같은 registry 파일로 수렴했거나 파일이 깨졌다."""

    def __init__(self, requested: str, stored: str | None, path: Path) -> None:
        super().__init__(f"{path.name} belongs to run_id {stored!r}, not {requested!r}")
        self.requested = requested
        self.stored = stored
        self.path = path


def registry_path(state_dir: Path, run_id: str) -> Path:
    return state_dir / "runs" / f"{SAFE_NAME.sub('_', run_id)}.json"


def _lock_path(state_dir: Path, run_id: str) -> Path:
    return state_dir / "locks" / f"run-{SAFE_NAME.sub('_', run_id)}.lock"


def _parse(path: Path, run_id: str) -> dict | None:
    """None이면 파일이 없다는 뜻. 깨진 파일은 예외다 — 없는 것과 다르다."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        registry = json.loads(raw)
    except json.JSONDecodeError as exc:
        # 깨진 registry를 빈 것으로 취급하면 남의 session 위에 덮어쓴다.
        raise RunIdCollision(run_id, None, path) from exc
    if not isinstance(registry, dict):
        raise RunIdCollision(run_id, None, path)
    return registry


def read(state_dir: Path, run_id: str) -> dict:
    path = registry_path(state_dir, run_id)
    registry = _parse(path, run_id)
    if registry is None:
        return {"schema_version": "1.0", "run_id": run_id, "providers": {}}
    stored = registry.get("run_id")
    # 이전 스키마 파일에는 run_id가 없을 수 있다. 없는 것은 충돌 근거가 아니다.
    if stored is not None and stored != run_id:
        raise RunIdCollision(run_id, stored, path)
    return registry


def claim(state_dir: Path, run_id: str) -> None:
    """소유권을 확정한다. read-time 확인과 달리 write lock 안에서 수행한다."""
    path = registry_path(state_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(state_dir, run_id)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as handle:
        lock.chmod(0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            registry = _parse(path, run_id)
            if registry is None:
                registry = {"schema_version": "1.0", "run_id": run_id, "providers": {}}
            stored = registry.get("run_id")
            if stored is not None and stored != run_id:
                raise RunIdCollision(run_id, stored, path)
            registry["run_id"] = run_id          # 이전 스키마 파일 입양
            registry.setdefault("providers", {})
            registry.setdefault("schema_version", "1.0")
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(registry), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def session_for(state_dir: Path, run_id: str, provider: str) -> str | None:
    return read(state_dir, run_id).get("providers", {}).get(provider, {}).get("session_id")
