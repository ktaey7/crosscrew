"""Read-only usage summary for schema 2.0 result envelopes (docs/usage-accounting.md).

Sums Claude invocation reports, but only one compatible cumulative snapshot
per Codex/Grok session. Missing scope/identity/counts remain unknown. Never
converts across providers or subscription quota, never writes state.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import uuid
import datetime as dt

import provider_usage

PROVIDERS = ("claude", "codex", "grok", "agy")
SOURCES = provider_usage.SOURCES
MAX_BYTES = 2 * 1024 * 1024
MAX_COUNT = 2**63 - 1


def load_result(path) -> dict | None:
    """Parsed envelope for a regular, small, well-formed result file; else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                return None
            raw = handle.read(MAX_BYTES + 1)
            after = os.fstat(handle.fileno())
            if len(raw) != info.st_size or (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                return None
            value = json.loads(raw)
    except (OSError, ValueError, RecursionError, TypeError):
        return None
    if (not isinstance(value, dict) or value.get("schema_version") != "2.0"
            or value.get("provider") not in PROVIDERS):
        return None
    status = value.get("status")
    if not isinstance(status, str) or not status:
        return None
    return value


def trusted_tokens(provider: str, usage) -> dict:
    """Token dict of a provider_terminal usage with allowlisted provenance, else {}."""
    value = provider_usage.validated_usage(provider, usage)
    return value["tokens"] if value else {}


def cumulative_snapshot(rows):
    """Select one observed vector that dominates all others; never mix fields.

    A counter reset, incompatible partial records, or decreasing counters make
    the whole session unresolved. Repeated equal snapshots count only once.
    """
    dated = []
    for tokens, timestamp in rows:
        try:
            date = dt.datetime.fromisoformat(timestamp)
            if date.tzinfo is None:
                continue
            dated.append((date, tokens))
        except (ValueError, TypeError):
            pass
    dated.sort(key=lambda row: row[0])
    for (_, previous), (_, later) in zip(dated, dated[1:]):
        if any(value is not None and later.get(key) is not None and later[key] < value
               for key, value in previous.items()):
            return None
    rows = [tokens for tokens, _ in rows]
    for candidate in reversed(rows):
        if all(all(value is None or (candidate.get(key) is not None and candidate[key] >= value)
                   for key, value in earlier.items()) for earlier in rows):
            return candidate
    return None


def summarize(paths) -> dict:
    groups = {provider: {"calls": 0, "failed_calls": 0, "sums": {}} for provider in PROVIDERS}
    for provider in provider_usage.FIELDS:
        groups[provider]["sums"] = {field: [None, 0] for field in provider_usage.FIELDS[provider]}
    files = invalid = duplicates = 0
    seen = set()
    cumulative = {p: {} for p in PROVIDERS}
    unknown = {p: 0 for p in PROVIDERS}
    covered = {p: 0 for p in PROVIDERS}
    for path in paths:
        files += 1
        result = load_result(path)
        if result is None:
            invalid += 1
            continue
        identity = (result.get("provider"), result.get("run_id"), result.get("started_at"))
        if not all(isinstance(v, str) and v for v in identity):
            info = os.stat(path)
            identity = (info.st_dev, info.st_ino)
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        provider = result["provider"]
        group = groups[provider]
        group["calls"] += 1
        group["failed_calls"] += result["status"] != "ok"
        tokens = trusted_tokens(provider, result.get("usage"))
        if provider_usage.AGGREGATION.get(provider) == "unverified":
            # Reported values are preserved in the envelope; their turn/session
            # scope is not yet established, so no totals are derived here.
            tokens = {}
        if provider_usage.AGGREGATION.get(provider) == "session_cumulative" and tokens:
            try:
                sid = str(uuid.UUID(result["session_id"]))
            except (ValueError, TypeError, KeyError, AttributeError):
                unknown[provider] += 1
                tokens = {}
            else:
                cumulative[provider].setdefault(sid, []).append((tokens, result.get("started_at")))
                covered[provider] += 1
                continue
        for field, cell in group["sums"].items():
            value = tokens.get(field)
            if type(value) is int and 0 <= value <= MAX_COUNT:
                cell[0] = (cell[0] or 0) + value
            else:
                cell[1] += 1
    providers = {}
    for provider, group in groups.items():
        unresolved = 0
        for rows in cumulative[provider].values():
            tokens = cumulative_snapshot(rows)
            if tokens is None:
                unresolved += 1
                tokens = {}
            for field, cell in group["sums"].items():
                value = tokens.get(field)
                if value is not None:
                    cell[0] = (cell[0] or 0) + value
                else:
                    cell[1] += len(rows)
        providers[provider] = {
            "calls": group["calls"],
            "failed_calls": group["failed_calls"],
            "tokens": {field: {"reported_sum": total, "missing_calls": missing}
                       for field, (total, missing) in group["sums"].items()},
        }
        if provider_usage.AGGREGATION.get(provider) == "session_cumulative":
            providers[provider]["aggregation"] = "session_cumulative"
            providers[provider]["sessions"] = len(cumulative[provider])
            providers[provider]["unknown_session_calls"] = unknown[provider]
            providers[provider]["unresolved_sessions"] = unresolved
            providers[provider]["cumulative_calls"] = covered[provider]
        elif provider_usage.AGGREGATION.get(provider) == "unverified":
            providers[provider]["aggregation"] = "unverified"
    return {"files": files, "invalid_files": invalid, "duplicate_files": duplicates, "providers": providers}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Summarize result envelope usage; read-only.")
    parser.add_argument("files", nargs="+", metavar="FILE")
    args = parser.parse_args(argv)
    report = summarize(args.files)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["invalid_files"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
