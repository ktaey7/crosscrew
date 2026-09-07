#!/usr/bin/env python3
"""Media capability lookups shared by the dispatcher, supervisor, and broker.

Three components validate a media request: `call_worker.py` (authoritative),
`worker_job.py` (before spawning a job), and `broker_protocol.py` (before an
escalated call leaves the sandbox). Each must agree on which provider can
generate which kind, so the answer lives here and is read from the registry
rather than restated in each place.
"""

from __future__ import annotations


def capabilities(config: dict, provider: str) -> dict:
    return config.get("providers", {}).get(provider, {}).get("media_capabilities", {}) or {}


def kinds(config: dict, provider: str) -> list[str]:
    return list(capabilities(config, provider).get("kinds", []))


def providers(config: dict) -> dict:
    """{provider: [kind, ...]} for every provider that declares a media capability."""
    return {
        name: list(entry["media_capabilities"]["kinds"])
        for name, entry in sorted(config.get("providers", {}).items())
        if entry.get("media_capabilities", {}).get("kinds")
    }


def validation_error(config: dict, provider: str, media_kind: str | None,
                     aspect_ratio: str | None, duration: int | None) -> tuple[str, str] | None:
    """Return (status, hint) when a media request contradicts the registry."""
    supported = kinds(config, provider)
    if not supported:
        return (
            "unsupported_profile",
            f"{provider} declares no media capability. Route generation to {providers(config)}.",
        )
    if media_kind and media_kind not in supported:
        return (
            "unsupported_media_kind",
            f"{provider} supports {supported}. Route {media_kind} to a provider that declares it.",
        )
    entry = capabilities(config, provider)
    if duration is not None and not entry.get("supports_duration"):
        return ("unsupported_media_option", f"{provider} does not accept a duration.")
    if aspect_ratio is not None and not entry.get("supports_aspect_ratio"):
        return ("unsupported_media_option", f"{provider} does not accept an aspect ratio.")
    if duration is not None:
        allowed = entry.get("durations")
        if allowed and duration not in allowed:
            return ("unsupported_media_option", f"{provider} accepts durations {allowed}.")
    return None
