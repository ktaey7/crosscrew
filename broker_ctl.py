#!/usr/bin/env python3
"""Operator CLI for the escalation broker: health checks and manual requests.

Hosts normally reach the broker implicitly through ``call_worker.py`` or
``worker_job.py``. This tool is for diagnostics and acceptance tests.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import broker_client
import broker_endpoint as endpoint

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CROSSCREW_BACKENDS_CONFIG", ROOT / "backends.json"))


def state_dir() -> Path:
    override = os.environ.get("CROSSCREW_STATE_DIR")
    if override:
        path = Path(override).expanduser()
    else:
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            configured = config.get("defaults", {}).get("state_dir", ".state")
        except (OSError, json.JSONDecodeError):
            configured = ".state"
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = ROOT / path
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("health", help="query /health")
    sub.add_parser("endpoint", help="print the registered endpoint record")
    send = sub.add_parser("send", help="send a typed request read from stdin or --json")
    send.add_argument("--json", help="request body; omit to read stdin")
    args = ap.parse_args()

    root = state_dir()

    if args.command == "endpoint":
        info = endpoint.read_endpoint(root)
        print(json.dumps(info or {"status": "no_endpoint"}, ensure_ascii=False, indent=2))
        return 0 if info else 69

    if args.command == "health":
        payload = broker_client.health(root)
        if payload is None:
            print(
                json.dumps(
                    {
                        "status": "broker_unavailable",
                        "hint": "Run broker_service.sh install, or check broker_service.sh logs.",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 69
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    raw = args.json if args.json else sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"status": "invalid_json", "error": str(exc)}))
        return 64
    try:
        result = broker_client.request(root, payload)
    except broker_client.BrokerUnavailable as exc:
        print(json.dumps({"status": "broker_unavailable", "error": str(exc)}, indent=2))
        return 69
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"ok", "started", "listening"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
