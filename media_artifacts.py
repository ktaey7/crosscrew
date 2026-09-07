#!/usr/bin/env python3
"""Collect and validate generated media artifacts from one fresh provider session.

Success is never taken from a worker's stdout. The host locates the file the
provider actually wrote, checks it is a regular unlinked file of an allowed MIME
type with sane dimensions, copies it without overwriting anything, and re-checks
the copy before hashing it. A provider that claims success without leaving a
verifiable artifact yields `artifact_missing`.

Two artifact sources exist today:

- ``grok_session``: Grok Imagine writes into the fresh session's
  ``images/`` or ``videos/`` directory.
- ``codex_generated_images``: Codex's built-in ``image_gen`` writes into
  ``~/.codex/generated_images/<thread-id>/``.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from urllib.parse import quote


IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
VIDEO_MIME_TYPES = {"video/mp4", "video/quicktime", "video/webm"}
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_VIDEO_BYTES = 1024 * 1024 * 1024


class ArtifactError(RuntimeError):
    """Raised when a generated artifact fails the media contract.

    ``code`` is the leading token of the message and lets the dispatcher tell
    three different situations apart instead of reporting one vague failure:
    the provider's storage root moved (our layout assumption broke), the
    session directory never appeared (generation did not run), or the file
    itself failed validation.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = str(message).split(":", 1)[0]


GROK_SESSIONS_ROOT = Path.home() / ".grok" / "sessions"
CODEX_IMAGES_ROOT = Path.home() / ".codex" / "generated_images"


def grok_session_dir(target: Path, session_id: str) -> Path | None:
    root = GROK_SESSIONS_ROOT
    direct = root / quote(str(target.resolve()), safe="") / session_id
    if direct.is_dir():
        return direct
    if not root.is_dir():
        return None
    matches = [path for path in root.glob(f"**/{session_id}") if path.is_dir()]
    return matches[0] if len(matches) == 1 else None


def _mime_type(path: Path) -> str:
    command = Path("/usr/bin/file")
    if command.is_file():
        completed = subprocess.run(
            [str(command), "--brief", "--mime-type", str(path)],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if completed.returncode == 0:
            return completed.stdout.strip().lower()
    return (mimetypes.guess_type(path.name)[0] or "application/octet-stream").lower()


def _image_dimensions(path: Path) -> dict:
    command = Path("/usr/bin/sips")
    if not command.is_file():
        raise ArtifactError("image_dimension_probe_unavailable")
    completed = subprocess.run(
        [str(command), "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        raise ArtifactError("image_dimension_probe_failed")
    width = re.search(r"pixelWidth:\s*(\d+)", completed.stdout)
    height = re.search(r"pixelHeight:\s*(\d+)", completed.stdout)
    if not width or not height or int(width.group(1)) < 1 or int(height.group(1)) < 1:
        raise ArtifactError("image_dimensions_invalid")
    return {"width": int(width.group(1)), "height": int(height.group(1))}


def _video_metadata(path: Path) -> dict:
    command = shutil.which("ffprobe")
    if not command:
        raise ArtifactError("video_probe_unavailable")
    completed = subprocess.run(
        [
            command,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,duration:format=duration",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise ArtifactError("video_probe_failed")
    try:
        payload = json.loads(completed.stdout)
        stream = payload.get("streams", [{}])[0]
        duration = float(stream.get("duration") or payload.get("format", {}).get("duration"))
        width = int(stream["width"])
        height = int(stream["height"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ArtifactError("video_metadata_invalid") from exc
    if width < 1 or height < 1 or duration <= 0:
        raise ArtifactError("video_metadata_invalid")
    return {"width": width, "height": height, "duration_seconds": round(duration, 3)}


def _validate_source(path: Path, kind: str) -> tuple[os.stat_result, str, dict]:
    try:
        details = path.lstat()
    except OSError as exc:
        raise ArtifactError("artifact_source_unreadable") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise ArtifactError("artifact_source_not_regular")
    if details.st_nlink != 1:
        raise ArtifactError("artifact_source_hardlinked")
    maximum = MAX_IMAGE_BYTES if kind == "image" else MAX_VIDEO_BYTES
    if details.st_size < 1 or details.st_size > maximum:
        raise ArtifactError("artifact_size_invalid")
    mime = _mime_type(path)
    allowed = IMAGE_MIME_TYPES if kind == "image" else VIDEO_MIME_TYPES
    if mime not in allowed:
        raise ArtifactError(f"artifact_mime_invalid:{mime}")
    metadata = _image_dimensions(path) if kind == "image" else _video_metadata(path)
    return details, mime, metadata


def codex_image_dir(session_id: str) -> Path | None:
    """Locate the per-thread directory Codex's image_gen writes into."""
    directory = CODEX_IMAGES_ROOT / session_id
    if directory.is_dir() and not directory.is_symlink():
        return directory
    return None


def _copy_exclusive(
    source: Path,
    output_dir: Path,
    session_id: str,
    ordinal: int,
    prefix: str = "grok-imagine",
) -> Path:
    suffix = source.suffix.lower() or ".bin"
    base = f"{prefix}-{session_id[:8]}-{ordinal}{suffix}"
    destination = output_dir / base
    counter = 1
    while destination.exists():
        destination = output_dir / f"{prefix}-{session_id[:8]}-{ordinal}-{counter}{suffix}"
        counter += 1
    source_handle = source.open("rb")
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as destination_handle:
            shutil.copyfileobj(source_handle, destination_handle, length=1024 * 1024)
    finally:
        source_handle.close()
    destination.chmod(0o600)
    return destination


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _collect_from_folder(
    *,
    folder: Path,
    relative_root: Path,
    session_id: str,
    output_dir: Path,
    media_kind: str,
    prefix: str,
) -> list[dict]:
    """Validate one session's generated files and copy the finals out."""
    if not folder.is_dir() or folder.is_symlink():
        return []
    candidates = sorted(path for path in folder.iterdir() if path.is_file())
    if len(candidates) > 1:
        # A single request must yield a single final artifact. More than one means
        # the session is not the clean provenance the contract assumes.
        raise ArtifactError("artifact_count_invalid")
    artifacts: list[dict] = []
    for ordinal, source in enumerate(candidates, start=1):
        details, mime, metadata = _validate_source(source, media_kind)
        destination = _copy_exclusive(source, output_dir, session_id, ordinal, prefix)
        copied_details, copied_mime, copied_metadata = _validate_source(destination, media_kind)
        if copied_details.st_size != details.st_size or copied_mime != mime or copied_metadata != metadata:
            destination.unlink(missing_ok=True)
            raise ArtifactError("artifact_copy_verification_failed")
        digest = _sha256(destination)
        artifacts.append(
            {
                "kind": media_kind,
                "path": str(destination),
                "source_session_relative": str(source.relative_to(relative_root)),
                "mime_type": mime,
                "bytes": details.st_size,
                "sha256": digest,
                **metadata,
            }
        )
    return artifacts


def collect_grok_artifacts(
    *,
    target: Path,
    session_id: str,
    output_dir: Path,
    media_kind: str,
) -> list[dict]:
    """Validate fresh-session Grok Imagine artifacts and copy finals out."""
    if not GROK_SESSIONS_ROOT.is_dir():
        raise ArtifactError(f"artifact_root_missing:grok_session:{GROK_SESSIONS_ROOT}")
    session_dir = grok_session_dir(target, session_id)
    if session_dir is None:
        raise ArtifactError(f"artifact_session_missing:grok_session:{session_id}")
    return _collect_from_folder(
        folder=session_dir / ("images" if media_kind == "image" else "videos"),
        relative_root=session_dir,
        session_id=session_id,
        output_dir=output_dir,
        media_kind=media_kind,
        prefix="grok-imagine",
    )


def collect_codex_artifacts(
    *,
    session_id: str,
    output_dir: Path,
    media_kind: str,
) -> list[dict]:
    """Validate one Codex thread's image_gen output and copy finals out."""
    if media_kind != "image":
        raise ArtifactError(f"artifact_kind_unsupported:{media_kind}")
    if not CODEX_IMAGES_ROOT.is_dir():
        raise ArtifactError(f"artifact_root_missing:codex_generated_images:{CODEX_IMAGES_ROOT}")
    directory = codex_image_dir(session_id)
    if directory is None:
        raise ArtifactError(f"artifact_session_missing:codex_generated_images:{session_id}")
    return _collect_from_folder(
        folder=directory,
        relative_root=directory,
        session_id=session_id,
        output_dir=output_dir,
        media_kind=media_kind,
        prefix="codex-imagegen",
    )


def collect_media_artifacts(
    *,
    artifact_source: str | None,
    target: Path,
    session_id: str | None,
    output_dir: Path,
    media_kind: str,
) -> list[dict]:
    """Dispatch to the collector the provider registry declares."""
    if not session_id:
        # Without a session id there is no verifiable provenance to search.
        return []
    if artifact_source == "grok_session":
        return collect_grok_artifacts(
            target=target,
            session_id=session_id,
            output_dir=output_dir,
            media_kind=media_kind,
        )
    if artifact_source == "codex_generated_images":
        return collect_codex_artifacts(
            session_id=session_id,
            output_dir=output_dir,
            media_kind=media_kind,
        )
    raise ArtifactError(f"artifact_source_unknown:{artifact_source}")
