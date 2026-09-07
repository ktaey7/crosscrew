"""Read-only waiting: never repair state, signal a worker, or print its answer."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

import projection

MAX_RESULT_BYTES = 16 * 1024 * 1024
MAX_META_BYTES = 64 * 1024
JOB_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def read_json(directory: Path, name: str, limit: int):
    """Read one regular file via a pinned directory FD, hashing those same bytes."""
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise ValueError("unsafe_or_oversized_file")
            raw = source.read(limit + 1)
            after = os.fstat(source.fileno())
            if len(raw) > limit or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("file_changed_during_read")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("not_an_object")
        return value, raw
    finally:
        os.close(parent)


def snapshot(root: Path, job_id: str, activity_reader=None) -> dict:
    if not JOB_ID.fullmatch(job_id) or job_id in {".", ".."}:
        raise ValueError("invalid_job_id")
    directory = root / "jobs" / job_id
    meta, _ = read_json(directory, "job.json", MAX_META_BYTES)
    result = None
    reference = None
    unreadable = False
    for name in ("result.json", "result.pending.json"):
        try:
            candidate, raw = read_json(directory, name, MAX_RESULT_BYTES)
            if not isinstance(candidate.get("status"), str) or not candidate["status"]:
                raise ValueError("missing_worker_status")
        except FileNotFoundError:
            continue
        except (OSError, ValueError, UnicodeError, RecursionError):
            unreadable = True
            continue
        result = candidate
        reference = {"path": str(directory.absolute() / name), "size_bytes": len(raw),
                     "sha256": hashlib.sha256(raw).hexdigest()}
        break
    lifecycle, worker, detail = projection.lifecycle(directory, meta, result=result)
    try:
        activity = activity_reader(directory, meta) if activity_reader and lifecycle == "running" else {}
    except Exception:
        activity = {"progress_degraded": True}
    return {"schema_version": "1.0", "job_id": job_id, "status": lifecycle, **activity,
            "lifecycle_status": lifecycle, "worker_status": worker, "detail": detail,
            "terminal": lifecycle in projection.TERMINAL, "result_ref": reference,
            "result_unreadable": unreadable and reference is None, "auto_canceled": False}


def observations(root: Path, job_id: str, interval: float = 2, timeout: float = 0, activity_reader=None):
    if not math.isfinite(interval) or interval < 0.05 or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("interval must be >= 0.05; timeout must be finite and >= 0")
    started = time.monotonic()
    while True:
        row = snapshot(root, job_id, activity_reader)
        elapsed = time.monotonic() - started
        row["wait_status"] = "finished" if row["terminal"] else (
            "timeout" if timeout and elapsed >= timeout else "waiting")
        yield row
        if row["wait_status"] != "waiting":
            return
        delay = min(interval, max(0, timeout - elapsed)) if timeout else interval
        time.sleep(delay)


def run(root: Path, job_id: str, interval: float, timeout: float, *, watch: bool = False, activity_reader=None) -> int:
    row = {"job_id": job_id, "lifecycle_status": "state_unconfirmed", "worker_status": None,
           "terminal": False, "result_ref": None, "auto_canceled": False}
    try:
        for row in observations(root, job_id, interval, timeout, activity_reader):
            if watch or row["wait_status"] != "waiting":
                print(json.dumps(row, ensure_ascii=False), flush=True)
        if row["wait_status"] == "timeout":
            return 124
        return {"completed": 0, "failed": 1, "canceled": 130}[row["lifecycle_status"]]
    except KeyboardInterrupt:
        row["wait_status"] = "interrupted"
        code = 130
    except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
        row["wait_status"] = "observation_error"
        # Do not return exception text or contents of malformed state files.
        code = 66
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return code
