"""Bounded provider activity snapshots. No prompts, answers, or error text on disk."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import stat
import time
import uuid

import job_wait

EVENTS = frozenset({"thread.started", "turn.started", "turn.completed", "turn.failed",
                    "item.started", "item.updated", "item.completed", "error"})
MAX_LINE_BYTES = 64 * 1024
MAX_SNAPSHOT_BYTES = 2048


def session_id(value):
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


class CodexDecoder:
    def __init__(self, accept):
        self.accept = accept
        self.buffer = bytearray()
        self.dropping = False
        self.dropped = False

    def feed(self, chunk: bytes):
        for part in chunk.splitlines(keepends=True):
            complete = part.endswith(b"\n")
            if not self.dropping and len(self.buffer) + len(part) <= MAX_LINE_BYTES:
                self.buffer.extend(part)
            else:
                self.buffer.clear()
                self.dropping = self.dropped = True
            if complete:
                if not self.dropping:
                    self._line(bytes(self.buffer))
                self.buffer.clear()
                self.dropping = False

    def _line(self, raw: bytes):
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            return
        if not isinstance(value, dict):
            return
        kind = value.get("type")
        if not isinstance(kind, str) or kind not in EVENTS:
            return
        sid = None
        if kind == "thread.started":
            thread = value.get("thread")
            sid = session_id(value.get("thread_id")) or session_id(
                thread.get("id") if isinstance(thread, dict) else None)
        self.accept(kind, sid)

    def finish(self):
        # EOF can terminate a valid final event without a newline.
        if self.buffer and not self.dropping:
            self._line(bytes(self.buffer))
        self.buffer.clear()


class Writer:
    """Single writer, atomic fixed-size snapshot. Failure never kills execution."""
    def __init__(self, env: dict):
        self.fd = None
        self.degraded = False
        self.last_write = 0.0
        self.data = {"schema_version": "1.0", "provider": "codex", "event_count": 0,
                     "stdout_bytes": 0, "last_event_type": None, "last_event_at": None,
                     "session_id": None, "observation_degraded": False}
        self.decoder = CodexDecoder(self.accept)
        try:
            path = Path(env["CROSSCREW_PROGRESS_FILE"])
            sidecar = Path(env["CROSSCREW_LAUNCHER_SIDECAR"])
            if (env.get("CROSSCREW_SUPERVISED") != "1" or not path.is_absolute()
                    or path.name != "progress.json" or path.parent != sidecar.parent
                    or sidecar.name != "launcher.json"):
                raise ValueError("not_a_supervised_progress_path")
            self.fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(self.fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("unsafe_job_directory")
            claim = os.open("progress.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600, dir_fd=self.fd)
            os.close(claim)
            self.publish()
        except (OSError, KeyError, ValueError):
            self.degraded = True
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None

    def accept(self, kind: str, sid: str | None):
        self.data["event_count"] += 1
        self.data["last_event_type"] = kind
        self.data["last_event_at"] = time.time()
        if sid:
            self.data["session_id"] = sid
        if sid or time.monotonic() - self.last_write >= 0.2:
            self.publish()

    def feed(self, chunk: bytes):
        self.data["stdout_bytes"] += len(chunk)
        self.decoder.feed(chunk)

    def publish(self):
        if self.fd is None:
            return
        name = ".progress-" + uuid.uuid4().hex
        try:
            self.data["observation_degraded"] = self.degraded or self.decoder.dropped
            raw = json.dumps(self.data, separators=(",", ":")).encode()
            if len(raw) > MAX_SNAPSHOT_BYTES:
                raise ValueError("snapshot_limit")
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self.fd)
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
            os.replace(name, "progress.json", src_dir_fd=self.fd, dst_dir_fd=self.fd)
            self.last_write = time.monotonic()
        except (OSError, ValueError):
            self.degraded = True
        finally:
            try:
                os.unlink(name, dir_fd=self.fd)
            except OSError:
                pass

    def close(self):
        self.decoder.finish()
        self.publish()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def read(directory: Path) -> dict:
    try:
        data, _ = job_wait.read_json(directory, "progress.json", MAX_SNAPSHOT_BYTES)
        count, size = data.get("event_count"), data.get("stdout_bytes")
        when = data.get("last_event_at")
        kind = data.get("last_event_type")
        if (data.get("provider") != "codex" or data.get("schema_version") != "1.0"
                or type(count) is not int or count < 1 or type(size) is not int or size < 0
                or type(when) not in (int, float) or not math.isfinite(when)
                or when < 0 or when > time.time() + 5 or not isinstance(kind, str) or kind not in EVENTS):
            return {}
        return {"event_count": count, "stdout_bytes": size, "last_event_at": when,
                "last_event_type": kind, "session_id": session_id(data.get("session_id")),
                "observation_degraded": data.get("observation_degraded") is True}
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {}
