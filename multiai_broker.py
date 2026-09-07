#!/usr/bin/env python3
"""Loopback escalation broker for the multi-AI backend adapter.

Why this exists
---------------
Codex, Grok, and agy each run their host session inside a sandbox. Spawning
another agent CLI from inside that sandbox fails for reasons the sandbox is
right to enforce: Claude needs Keychain access for OAuth refresh, and Grok nests
a second sandbox of its own. The adapter used to fail closed with
``needs_host_escalation`` and ask the user to re-run the command by hand.

This broker turns that manual round trip into one automatic hop. It runs outside
every host sandbox under launchd and accepts *typed* requests over loopback,
which the sandboxed hosts can reach because their policies allow local network
access. It never accepts an argv: ``broker_protocol.validate`` constrains every
field and ``broker_protocol.build_argv`` constructs the command.

Trust boundary
--------------
The broker's vocabulary is exactly the adapter's CLI, so the privilege it grants
is exactly the privilege the adapter contract already defines: a worker runs
under its profile guard, scoped to a target directory that must already exist.
It is not a general command runner. Access requires a mode-0600 shared secret,
the listener binds to 127.0.0.1 only, and every request is logged.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hmac
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer

import broker_endpoint as endpoint
import broker_protocol as protocol

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CROSSCREW_BACKENDS_CONFIG", ROOT / "backends.json"))
BROKER_VERSION = "1.0"
MAX_BODY_BYTES = 65536
JOB_ACTION_TIMEOUT = 120.0
DEFAULT_CALL_TIMEOUT = 3600.0

STARTED_AT = time.time()
LOG_LOCK = threading.Lock()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"defaults": {}}


def resolve_state_dir(config: dict) -> Path:
    override = os.environ.get("CROSSCREW_STATE_DIR")
    if override:
        state_dir = Path(override).expanduser()
    else:
        configured = config.get("defaults", {}).get("state_dir", ".state")
        state_dir = Path(configured).expanduser()
        if not state_dir.is_absolute():
            state_dir = ROOT / state_dir
    endpoint.ensure_private_dir(state_dir)
    return state_dir


class BrokerState:
    def __init__(self, state_dir: Path, token: str, call_timeout: float) -> None:
        self.state_dir = state_dir
        self.token = token
        self.call_timeout = call_timeout


def log_request(state: BrokerState, entry: dict) -> None:
    path = endpoint.request_log_path(state.state_dir)
    line = json.dumps({"at": utc_now(), **entry}, ensure_ascii=False)
    with LOG_LOCK:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def run_adapter(request: dict, state: BrokerState) -> dict:
    argv = protocol.build_argv(request)
    timeout = JOB_ACTION_TIMEOUT if request["action"] != "call" else state.call_timeout
    env = os.environ.copy()
    # Lets the adapter refuse to recurse back into the broker.
    env["CROSSCREW_BROKER_CHILD"] = "1"
    env.pop("CROSSCREW_SUPERVISED", None)
    env.pop("CROSSCREW_LAUNCHER_SIDECAR", None)
    env.pop("CROSSCREW_LAUNCHER_TOKEN", None)
    cwd = request.get("target") or ROOT
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {
            "schema_version": "1.0",
            "status": "broker_call_timeout",
            "exit_code": 124,
            "retryable": True,
            "timeout_seconds": timeout,
            "hint": (
                "The broker bounds synchronous calls. Use action=start for long "
                "work; it returns a job_id immediately and never hard-times-out."
            ),
        }
    except OSError as exc:
        return {
            "schema_version": "1.0",
            "status": "broker_spawn_failed",
            "exit_code": 127,
            "retryable": False,
            "error": str(exc),
        }

    stdout = completed.stdout.strip()
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        return {
            "schema_version": "1.0",
            "status": "broker_bad_envelope",
            "exit_code": completed.returncode or 1,
            "retryable": True,
            "stdout_head": stdout[:2000],
            "stderr_head": completed.stderr.strip()[:2000],
        }
    if not isinstance(payload, dict):
        return {
            "schema_version": "1.0",
            "status": "broker_bad_envelope",
            "exit_code": completed.returncode or 1,
            "retryable": True,
            "stdout_head": stdout[:2000],
        }
    payload["transport"] = "broker"
    payload["escalated_via"] = "broker"
    return payload


class Handler(BaseHTTPRequestHandler):
    server_version = f"multiai-broker/{BROKER_VERSION}"
    protocol_version = "HTTP/1.1"
    broker_state: BrokerState

    def log_message(self, *_args) -> None:  # noqa: D401 - stdlib access log is noise
        return

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Multiai-Token", "")
        return bool(supplied) and hmac.compare_digest(supplied, self.broker_state.token)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path != "/health":
            self._send(404, {"status": "not_found"})
            return
        self._send(
            200,
            {
                "schema_version": "1.0",
                "status": "ok",
                "broker_version": BROKER_VERSION,
                "pid": os.getpid(),
                "uptime_seconds": round(time.time() - STARTED_AT, 3),
                "state_dir": str(self.broker_state.state_dir),
                "actions": list(protocol.ACTIONS),
                "call_timeout_seconds": self.broker_state.call_timeout,
            },
        )

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path != "/request":
            self._send(404, {"status": "not_found"})
            return
        if not self._authorized():
            log_request(self.broker_state, {"outcome": "unauthorized", "path": self.path})
            self._send(401, {"status": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, {"status": "invalid_length"})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send(413, {"status": "invalid_body_size", "max_bytes": MAX_BODY_BYTES})
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send(400, {"status": "invalid_json", "error": str(exc)})
            return
        try:
            request = protocol.validate(payload)
        except protocol.RequestError as exc:
            log_request(
                self.broker_state,
                {"outcome": "rejected", "field": exc.field, "detail": exc.detail},
            )
            self._send(
                400,
                {
                    "schema_version": "1.0",
                    "status": "broker_request_rejected",
                    "exit_code": 64,
                    "retryable": False,
                    "field": exc.field,
                    "error": exc.detail,
                },
            )
            return

        began = time.monotonic()
        result = run_adapter(request, self.broker_state)
        log_request(
            self.broker_state,
            {
                "outcome": "executed",
                "action": request["action"],
                "provider": request.get("provider"),
                "host": request.get("host"),
                "profile": request.get("profile"),
                "mode": request.get("mode"),
                "target": str(request.get("target") or ""),
                "job_id": result.get("job_id") or request.get("job_id"),
                "result_status": result.get("status"),
                "duration_seconds": round(time.monotonic() - began, 3),
            },
        )
        self._send(200, result)


class LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True
    address_family = socket.AF_INET

    def server_bind(self):
        # HTTPServer normally performs reverse DNS here. This loopback-only
        # listener has a fixed name and must not depend on a host DNS resolver.
        TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


def serve(state_dir: Path, port: int, call_timeout: float) -> int:
    token = endpoint.read_token(state_dir) or endpoint.issue_token(state_dir)
    state = BrokerState(state_dir, token, call_timeout)
    Handler.broker_state = state

    try:
        httpd = LoopbackServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        print(json.dumps({"status": "broker_bind_failed", "error": str(exc)}), file=sys.stderr)
        return 73

    bound_port = httpd.server_address[1]
    endpoint.write_endpoint(state_dir, bound_port, os.getpid(), utc_now(),
                            protocol.contract_hash())
    print(
        json.dumps(
            {
                "status": "listening",
                "host": "127.0.0.1",
                "port": bound_port,
                "pid": os.getpid(),
                "state_dir": str(state_dir),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    def shutdown(_signum, _frame) -> None:
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        endpoint.clear_endpoint(state_dir)
        httpd.server_close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=0, help="0 lets the OS choose a free port")
    ap.add_argument("--call-timeout", type=float, default=DEFAULT_CALL_TIMEOUT)
    ap.add_argument("--rotate-token", action="store_true", help="issue a new shared secret and exit")
    args = ap.parse_args()
    config = load_config()
    state_dir = resolve_state_dir(config)
    if args.rotate_token:
        endpoint.issue_token(state_dir)
        print(json.dumps({"status": "token_rotated", "path": str(endpoint.token_path(state_dir))}))
        return 0
    configured_timeout = config.get("defaults", {}).get("broker_call_timeout_seconds")
    call_timeout = float(configured_timeout) if configured_timeout else args.call_timeout
    return serve(state_dir, args.port, call_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
