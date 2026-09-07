#!/usr/bin/env python3
"""Reasoning-effort capability lookups shared by the dispatcher, supervisor, and broker.

Three components decide whether a requested reasoning effort may be sent to a
provider: `call_worker.py` (authoritative), `worker_job.py` (before spawning a
job), and `broker_protocol.py` (before an escalated call leaves the sandbox).
They must agree, so the answer is read from the registry here instead of being
restated in each place.

Only a provider that *declares* `reasoning_efforts` accepts one. An undeclared
provider is a rejection, never a silent downgrade to whatever that provider has
configured locally: a caller who asked for a specific effort and did not get it
must be told, not quietly served something else.
"""

from __future__ import annotations


def capabilities(config: dict, provider: str) -> dict:
    return config.get("providers", {}).get(provider, {}).get("reasoning_efforts", {}) or {}


def values(config: dict, provider: str) -> list[str]:
    return list(capabilities(config, provider).get("values", []))


def config_key(config: dict, provider: str) -> str | None:
    return capabilities(config, provider).get("config_key")


def providers(config: dict) -> dict:
    """{provider: [effort, ...]} for every provider that declares the capability."""
    return {
        name: list(entry["reasoning_efforts"]["values"])
        for name, entry in sorted(config.get("providers", {}).items())
        if entry.get("reasoning_efforts", {}).get("values")
    }


def validation_error(config: dict, provider: str, effort: str | None) -> tuple[str, str] | None:
    """Return (status, hint) when an effort request contradicts the registry."""
    if effort is None:
        return None
    supported = values(config, provider)
    if not supported:
        return (
            "unsupported_effort",
            f"{provider} declares no reasoning effort control. "
            f"Providers that do: {providers(config)}.",
        )
    if effort not in supported:
        return (
            "unsupported_effort",
            f"{provider} accepts {supported}.",
        )
    return None
