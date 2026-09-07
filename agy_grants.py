#!/usr/bin/env python3
"""Crash-safe temporary agy project grants."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
from typing import TextIO
import uuid


@dataclass
class AgyProjectGrant:
    project_id: str
    project_path: Path
    lock_path: Path
    lock_handle: TextIO


def projects_dir() -> Path:
    override = os.environ.get("CROSSCREW_AGY_PROJECTS_DIR")
    return Path(override).expanduser() if override else Path.home() / ".gemini" / "config" / "projects"


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def secure_write_json(path: Path, payload: dict) -> None:
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp, path)
        path.chmod(0o600)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def cleanup_orphan_projects(directory: Path | None = None) -> list[str]:
    """Remove grants whose owner no longer holds the matching flock."""
    root = directory or projects_dir()
    if not root.exists():
        return []
    removed: list[str] = []
    for project_path in sorted(root.glob("multi-ai-work-*.json")):
        lock_path = project_path.with_suffix(".lock")
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a+", encoding="utf-8") as handle:
                lock_path.chmod(0o600)
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                project_path.unlink(missing_ok=True)
                removed.append(project_path.stem)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            lock_path.unlink(missing_ok=True)
        except OSError:
            continue
    return removed


def create_work_project(target: Path) -> AgyProjectGrant:
    root = projects_dir()
    ensure_private_dir(root)
    cleanup_orphan_projects(root)
    project_id = f"multi-ai-work-{uuid.uuid4()}"
    project_path = root / f"{project_id}.json"
    lock_path = root / f"{project_id}.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    handle = os.fdopen(fd, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        secure_write_json(
            project_path,
            {
                "id": project_id,
                "name": f"Multi-AI work: {target.name or 'target'}",
                "projectResources": {"resources": [{"folderUri": target.as_uri()}]},
                "permissionGrants": {
                    "permissionGrants": {"allow": [f"write_file({target})"]}
                },
            },
        )
        return AgyProjectGrant(project_id, project_path, lock_path, handle)
    except Exception:
        handle.close()
        project_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)
        raise


def release_work_project(grant: AgyProjectGrant) -> None:
    try:
        grant.project_path.unlink(missing_ok=True)
        fcntl.flock(grant.lock_handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        grant.lock_handle.close()
        try:
            grant.lock_path.unlink(missing_ok=True)
        except OSError:
            pass
