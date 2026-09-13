"""Read-only comparison of explicitly supplied, independently accepted runs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys

import usage_summary


ARMS = ("direct", "delegated")
ROLES = ("host_results", "worker_results")
TOKEN_KINDS = ("uncached_input_tokens", "cached_input_tokens", "output_tokens")


def _validated_paths(spec: dict, base_dir: Path) -> dict:
    """Validate shape and cross-list overlap without requiring files to exist."""
    if (not isinstance(spec, dict) or type(spec.get("schema_version")) is not int
            or spec["schema_version"] != 1):
        raise ValueError("invalid_manifest")

    # Validate the entire shape before inspecting any paths.
    for name in ARMS:
        arm = spec.get(name)
        if not isinstance(arm, dict) or type(arm.get("accepted")) is not bool:
            raise ValueError("invalid_manifest")
        for role in ROLES:
            paths = arm.get(role)
            if (not isinstance(paths, list) or (role == "host_results" and not paths)
                    or any(not isinstance(path, str) or not path for path in paths)):
                raise ValueError("invalid_manifest")

    base = Path(base_dir).absolute()
    prepared = {}
    owners = {}
    for name in ARMS:
        prepared[name] = {}
        for role in ROLES:
            owner = (name, role)
            prepared[name][role] = []
            for value in spec[name][role]:
                path = base / value
                # Keep the original anchored path for the frozen reader's file
                # rules (including O_NOFOLLOW); resolve only for overlap checks.
                try:
                    canonical = str(path.resolve())
                except (OSError, RuntimeError, ValueError):
                    # Unreadable/invalid result paths belong in the summary.
                    canonical = os.path.abspath(path)
                identities = [("path", canonical)]
                try:
                    info = path.stat()
                except (OSError, ValueError):
                    pass
                else:
                    identities.append(("inode", info.st_dev, info.st_ino))
                for identity in identities:
                    if identity in owners and owners[identity] != owner:
                        raise ValueError("invalid_manifest")
                    owners[identity] = owner
                prepared[name][role].append(path)
    return prepared


def _codex_tokens(host: dict) -> dict:
    """Return a complete observed vector, or an entirely unknown vector."""
    unknown = dict.fromkeys(TOKEN_KINDS)
    codex = host["providers"]["codex"]
    if (host["invalid_files"] != 0 or codex["calls"] == 0
            or any(group["calls"] != 0 for provider, group in host["providers"].items()
                   if provider != "codex")
            or codex["failed_calls"] != 0 or codex["unknown_session_calls"] != 0
            or codex["unresolved_sessions"] != 0):
        return unknown
    counts = {}
    for kind in ("input_tokens", "cached_input_tokens", "output_tokens"):
        cell = codex["tokens"][kind]
        value = cell["reported_sum"]
        if cell["missing_calls"] != 0 or type(value) is not int or value < 0:
            return unknown
        counts[kind] = value
    if counts["cached_input_tokens"] > counts["input_tokens"]:
        return unknown
    return {
        "uncached_input_tokens": counts["input_tokens"] - counts["cached_input_tokens"],
        "cached_input_tokens": counts["cached_input_tokens"],
        "output_tokens": counts["output_tokens"],
    }


def build_report(spec: dict, base_dir: Path) -> dict:
    """Compare host consumption using only the four explicitly supplied lists.

    Invalid manifest shapes/overlap raise ValueError. Result file errors and
    incomplete usage remain visible in the unmodified usage summaries.
    """
    paths = _validated_paths(spec, base_dir)
    arms = {}
    for name in ARMS:
        host = usage_summary.summarize(paths[name]["host_results"])
        workers = usage_summary.summarize(paths[name]["worker_results"])
        arms[name] = {
            "accepted": spec[name]["accepted"],
            "host": host,
            "workers": workers,
            "codex_tokens": _codex_tokens(host),
        }

    reasons = []
    for name in ARMS:
        if not arms[name]["accepted"]:
            reasons.append(f"{name}_not_accepted")
    for name in ARMS:
        if arms[name]["codex_tokens"]["uncached_input_tokens"] is None:
            reasons.append(f"{name}_host_usage_incomplete")
    for name in ARMS:
        if arms[name]["workers"]["invalid_files"] > 0:
            reasons.append(f"{name}_worker_result_invalid")
    for name in ARMS:
        if any(group["failed_calls"] > 0
               for group in arms[name]["workers"]["providers"].values()):
            reasons.append(f"{name}_worker_failed")

    savings = {kind: {"absolute": None, "percent": None} for kind in TOKEN_KINDS}
    if not reasons:
        for kind in TOKEN_KINDS:
            direct = arms["direct"]["codex_tokens"][kind]
            delegated = arms["delegated"]["codex_tokens"][kind]
            difference = direct - delegated
            savings[kind] = {
                "absolute": difference,
                "percent": round(100 * difference / direct, 6) if direct else None,
            }
    return {"schema_version": 1, "eligible": not reasons, "reasons": reasons,
            "arms": arms, "savings": savings}


def _reject_json_constant(value: str):
    # Python's decoder otherwise accepts NaN/Infinity, which are not JSON.
    raise ValueError("invalid_manifest")


def _load_manifest(path: Path):
    # Avoid waiting on nonregular inputs such as FIFOs. Manifest symlinks are
    # permitted; result-file rules remain owned by usage_summary.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("invalid_manifest")
        return json.load(stream, parse_constant=_reject_json_constant)


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) != 1:
            raise ValueError("invalid_manifest")
        manifest = Path(args[0])
        spec = _load_manifest(manifest)
        report = build_report(spec, manifest.absolute().parent)
    except (OSError, ValueError, TypeError, RecursionError):
        print('{"error":"invalid_manifest"}')
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["eligible"] else 1


if __name__ == "__main__":
    sys.exit(main())
