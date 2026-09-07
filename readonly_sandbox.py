#!/usr/bin/env python3
"""OS-level write confinement for providers that cannot enforce it themselves.

Why this exists
---------------
The registry used to claim ``sandboxed_target_read_only`` for agy's review and
research profiles. A live probe on 2026-07-25 disproved that: under
``--mode plan --sandbox`` agy created a new file inside the target, overwrote an
existing one, and wrote a file *outside* the target. agy's ``--sandbox`` flag
restricts terminal commands, not its file-writing tools, so the policy name was
describing a mechanism that did not exist.

Rather than downgrade the claim to a comment, this module supplies the missing
mechanism with a macOS seatbelt profile:

- ``review`` / ``research``: every write is denied except the provider's own
  state directories and temporary space. The target is genuinely read-only.
- ``work``: writes are additionally allowed inside the target, and denied
  everywhere else, so the target boundary is enforced rather than requested.

``sandbox-exec`` is formally deprecated but is the only user-space confinement
available here. When it is unavailable the wrapper is skipped and the caller
reports the weaker policy instead of silently claiming the stronger one.
"""

from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil

READ_ONLY_PROFILES = ("review", "research")

# Paths every confined provider needs regardless of profile: character devices
# for stdio plus the system temporary areas that back $TMPDIR.
#
# Deliberately excludes /tmp and /private/tmp. Those are broad enough that a
# target living under them would become writable and silently defeat the
# read-only profile -- which is exactly what an earlier revision did.
COMMON_WRITE_PATHS = (
    "/private/var/folders",
    "/var/folders",
)
COMMON_WRITE_LITERALS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/zero")
COMMON_WRITE_REGEXES = (r"^/dev/tty", r"^/dev/fd/", r"^/dev/dtracehelper")


def supported() -> bool:
    return platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None


def _sbpl_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _expand(paths: list[str]) -> list[str]:
    resolved: list[str] = []
    for raw in paths:
        expanded = os.path.expanduser(os.path.expandvars(raw))
        if not expanded.startswith("/"):
            continue
        resolved.append(expanded)
        # Confinement is evaluated against the real path, so a symlinked
        # location such as /var/folders must be listed alongside its target.
        try:
            real = str(Path(expanded).resolve())
        except OSError:
            continue
        if real != expanded:
            resolved.append(real)
    seen: set[str] = set()
    unique: list[str] = []
    for path in resolved:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def profile_text(write_paths: list[str]) -> str:
    """Build a last-match-wins SBPL profile that denies all other writes."""
    lines = [
        "(version 1)",
        ";; Reads and process execution stay open; only writes are confined.",
        "(allow default)",
        "(deny file-write*)",
        "(allow file-write*",
    ]
    for path in _expand(write_paths):
        lines.append(f"  (subpath {_sbpl_string(path)})")
    for literal in COMMON_WRITE_LITERALS:
        lines.append(f"  (literal {_sbpl_string(literal)})")
    for regex in COMMON_WRITE_REGEXES:
        lines.append(f'  (regex #{_sbpl_string(regex)})')
    lines.append(")")
    return "\n".join(lines) + "\n"


def write_paths_for(
    provider_config: dict,
    profile: str,
    target: Path,
    temp_dir: Path,
) -> list[str]:
    sandbox_config = provider_config.get("os_sandbox", {})
    paths = list(sandbox_config.get("state_write_paths", []))
    paths.extend(COMMON_WRITE_PATHS)
    paths.append(str(temp_dir))
    if profile not in READ_ONLY_PROFILES:
        paths.append(str(target))
    return paths


def target_inside_writable_scope(
    provider_config: dict,
    profile: str,
    target: Path,
    temp_dir: Path,
) -> bool:
    """True when a read-only profile cannot actually protect this target.

    The provider needs its own state and temporary space to stay writable, so a
    target that happens to live inside one of those trees cannot be confined.
    Callers surface this instead of implying a guarantee that does not hold.
    """
    if profile not in READ_ONLY_PROFILES:
        return False
    allowed = _expand(
        list(provider_config.get("os_sandbox", {}).get("state_write_paths", []))
        + list(COMMON_WRITE_PATHS)
        + [str(temp_dir)]
    )
    try:
        resolved = Path(target).resolve()
    except OSError:
        return False
    for path in allowed:
        try:
            resolved.relative_to(Path(path))
        except ValueError:
            continue
        return True
    return False


def enabled_for(provider_config: dict, profile: str) -> bool:
    sandbox_config = provider_config.get("os_sandbox", {})
    if not sandbox_config.get("enabled"):
        return False
    profiles = sandbox_config.get("profiles")
    if profiles is not None and profile not in profiles:
        return False
    return supported()


def wrap(
    argv: list[str],
    provider_config: dict,
    profile: str,
    target: Path,
    temp_dir: Path,
) -> tuple[list[str], str | None]:
    """Return (argv, applied_policy). argv is unchanged when confinement is off."""
    if not enabled_for(provider_config, profile):
        return argv, None
    paths = write_paths_for(provider_config, profile, target, temp_dir)
    profile_path = temp_dir / "readonly.sb"
    fd = os.open(profile_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(profile_text(paths))
    applied = (
        "os_sandbox_no_write" if profile in READ_ONLY_PROFILES else "os_sandbox_target_write"
    )
    return ["sandbox-exec", "-f", str(profile_path), *argv], applied
