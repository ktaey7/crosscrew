#!/usr/bin/env python3
"""Safely install or inspect Grok Build private video-output configuration."""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import urlparse

from grok_media_privacy import grok_home, video_privacy_preflight


# These markers are written into ~/.grok/config.toml and locate an existing
# block on every later run. They are an identity, not a path: renaming them
# orphans the installed block, so the layer would re-append a duplicate or
# report the storage as unconfigured. They deliberately kept the old
# "multi-ai-backends" spelling when the layer moved on 2026-07-26.
BEGIN_MARKER = "# BEGIN multi-ai-backends grok private video output"
END_MARKER = "# END multi-ai-backends grok private video output"
BUCKET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,61}[A-Za-z0-9]$")
REGION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,126}/$")


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _validate_public_args(bucket: str, endpoint: str, region: str, key_prefix: str) -> None:
    parsed = urlparse(endpoint)
    if not BUCKET_RE.fullmatch(bucket):
        raise ValueError("bucket must be a valid S3 bucket name")
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
        raise ValueError("endpoint must be an HTTPS origin without a path")
    if not REGION_RE.fullmatch(region):
        raise ValueError("region is invalid")
    if not PREFIX_RE.fullmatch(key_prefix) or ".." in key_prefix.split("/"):
        raise ValueError("key-prefix must be a relative folder ending in '/'")


def _credentials() -> tuple[str, str]:
    access = os.environ.pop("GROK_VIDEO_S3_ACCESS_KEY_ID", "").strip()
    secret = os.environ.pop("GROK_VIDEO_S3_SECRET_ACCESS_KEY", "").strip()
    if bool(access) != bool(secret):
        raise ValueError("set both GROK_VIDEO_S3_ACCESS_KEY_ID and GROK_VIDEO_S3_SECRET_ACCESS_KEY")
    if not access:
        access = getpass.getpass("S3 access key ID: ").strip()
        secret = getpass.getpass("S3 secret access key: ").strip()
    if not access or not secret:
        raise ValueError("S3 credentials cannot be empty")
    return access, secret


def _render_block(
    *,
    bucket: str,
    endpoint: str,
    region: str,
    key_prefix: str,
    access_key_id: str,
    secret_access_key: str,
) -> str:
    return f"""{BEGIN_MARKER}
[tools]
disable_zdr_incompatible_tools = true

[tools.zdr_video_output_s3]
bucket = {_toml_string(bucket)}
endpoint = {_toml_string(endpoint.rstrip('/'))}
region = {_toml_string(region)}
key_prefix = {_toml_string(key_prefix)}

[tools.zdr_video_output_s3.read_write]
access_key_id = {_toml_string(access_key_id)}
secret_access_key = {_toml_string(secret_access_key)}
{END_MARKER}
"""


def _replace_managed_block(existing: str, block: str) -> str:
    begin = existing.find(BEGIN_MARKER)
    end = existing.find(END_MARKER)
    if begin >= 0 or end >= 0:
        if begin < 0 or end < begin:
            raise ValueError("managed Grok video-output markers are incomplete")
        end += len(END_MARKER)
        suffix = existing[end:].lstrip("\n")
        prefix = existing[:begin].rstrip()
        return "\n\n".join(part for part in (prefix, block.rstrip(), suffix.rstrip()) if part) + "\n"
    if re.search(r"(?m)^\s*\[tools(?:\.|\])", existing):
        raise ValueError("an unmanaged [tools] table already exists; merge manually instead of overwriting it")
    prefix = existing.rstrip()
    return (prefix + "\n\n" if prefix else "") + block


def _atomic_private_write(path: Path, content: str) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    backup = None
    if path.exists():
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = path.with_name(f"{path.name}.bak.{stamp}")
        shutil.copy2(path, backup)
        backup.chmod(0o600)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(0o600)
        os.replace(temp, path)
        path.chmod(0o600)
    finally:
        temp.unlink(missing_ok=True)
    return backup


def install(args: argparse.Namespace) -> int:
    _validate_public_args(args.bucket, args.endpoint, args.region, args.key_prefix)
    access, secret = _credentials()
    config = grok_home() / "config.toml"
    existing = config.read_text(encoding="utf-8") if config.exists() else ""
    block = _render_block(
        bucket=args.bucket,
        endpoint=args.endpoint,
        region=args.region,
        key_prefix=args.key_prefix,
        access_key_id=access,
        secret_access_key=secret,
    )
    updated = _replace_managed_block(existing, block)
    backup = _atomic_private_write(config, updated)
    report = video_privacy_preflight()
    payload = {
        "status": "configured" if report["private_output_configured"] else "config_invalid",
        "config_path": str(config),
        "backup_path": str(backup) if backup else None,
        **report,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "configured" else 78


def status() -> int:
    report = video_privacy_preflight()
    print(json.dumps({"status": "ready" if report["ready"] else "needs_configuration", **report}, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 78


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    actions = value.add_subparsers(dest="action", required=True)
    actions.add_parser("status")
    setup = actions.add_parser("install")
    setup.add_argument("--bucket", required=True)
    setup.add_argument("--endpoint", required=True)
    setup.add_argument("--region", required=True)
    setup.add_argument("--key-prefix", default="grok-videos/")
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        return status() if args.action == "status" else install(args)
    except (OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"status": "config_error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
