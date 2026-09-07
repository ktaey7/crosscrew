#!/usr/bin/env python3
"""Non-destructive job supervisor for long-running multi-AI workers."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

from agy_grants import cleanup_orphan_projects
import billing
import broker_client
from grok_media_privacy import video_privacy_preflight
import media_registry
import effort_registry
import mission_log
import projection
import job_wait
import progress
import run_registry


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CROSSCREW_BACKENDS_CONFIG", ROOT / "backends.json"))
PROVIDERS = ("claude", "codex", "grok", "agy")
HOSTS = ("claude", "codex", "grok", "agy", "shell")
ROUTE_PRECEDENCE = (
    "requires_host_escalation",
    "broker",
    "in_process",
    "companion_preferred",
    "direct",
)
ACTIVE_STATUSES = {"starting", "running"}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def route_for(config: dict, host: str, provider: str, profile: str, mode: str) -> str:
    """Resolve the effective transport; strictest matrix entry wins."""
    candidates = (
        config.get("host_matrix", {}).get(host, {}).get(provider),
        config.get("profile_host_matrix", {}).get(host, {}).get(provider, {}).get(profile),
        config.get("mode_host_matrix", {}).get(host, {}).get(provider, {}).get(mode),
    )
    present = {route for route in candidates if route in ROUTE_PRECEDENCE}
    for candidate in ROUTE_PRECEDENCE:
        if candidate in present:
            return candidate
    return "direct"


def requires_host_escalation(config: dict, host: str, provider: str, profile: str, mode: str) -> bool:
    return route_for(config, host, provider, profile, mode) in {
        "broker",
        "requires_host_escalation",
    }


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def state_dir(config: dict) -> Path:
    override = os.environ.get("CROSSCREW_STATE_DIR")
    if override:
        path = Path(override).expanduser()
    else:
        path = Path(config["defaults"].get("state_dir", ".state")).expanduser()
        if not path.is_absolute():
            path = ROOT / path
    ensure_private_dir(path)
    ensure_private_dir(path / "jobs")
    for directory in (path / "jobs").iterdir():
        if not directory.is_dir():
            continue
        try:
            directory.chmod(0o700)
            for child in directory.iterdir():
                child.chmod(0o700 if child.is_dir() else 0o600)
        except OSError:
            pass
    return path


def safe_job_id(value: str) -> str:
    if not value or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for ch in value):
        raise ValueError("job ID may contain only letters, numbers, dot, underscore, and hyphen")
    return value


def job_path(root: Path, job_id: str) -> Path:
    return root / "jobs" / safe_job_id(job_id)


def secure_write_text(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    path.chmod(0o600)


def private_text_handle(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


def atomic_json(path: Path, payload: dict) -> None:
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    secure_write_text(temp, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.replace(temp, path)
    path.chmod(0o600)


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def launcher_lock_held(path: Path) -> bool:
    if not path.is_file():
        return False
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def identity_status(meta: dict) -> str:
    raw_pid = meta.get("pid")
    if raw_pid is None:
        return "starting"
    pid = int(raw_pid)
    expected_pgid = meta.get("pgid")
    launcher_token = meta.get("launcher_token")
    lock_path = Path(meta["lock_path"]) if meta.get("lock_path") else None
    sidecar_path = Path(meta["sidecar_path"]) if meta.get("sidecar_path") else None
    if expected_pgid is None or not launcher_token or lock_path is None or sidecar_path is None:
        return "unverified" if process_alive(pid) else "dead"
    sidecar = parse_result(sidecar_path)
    if not sidecar:
        return "unverified" if process_alive(pid) else "dead"
    if (
        sidecar.get("launcher_token") != launcher_token
        or int(sidecar.get("pid", -1)) != pid
        or int(sidecar.get("pgid", -1)) != int(expected_pgid)
    ):
        return "mismatch"
    if launcher_lock_held(lock_path):
        return "verified"
    return "dead"


def latest_mtime(paths: list[Path], fallback: float) -> float:
    values = [fallback]
    for path in paths:
        try:
            values.append(path.stat().st_mtime)
        except OSError:
            pass
    return max(values)


def parse_result(path: Path) -> dict | None:
    try:
        if path.stat().st_size == 0:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def recover_meta(directory: Path, meta: dict) -> dict:
    if meta.get("pid") is not None:
        return meta
    sidecar = parse_result(directory / "launcher.json")
    if (
        not sidecar
        or not sidecar.get("pid")
        or sidecar.get("launcher_token") != meta.get("launcher_token")
    ):
        return meta
    recovered = dict(meta)
    recovered.update(
        {
            "state": "running",
            "pid": sidecar["pid"],
            "pgid": sidecar.get("pgid"),
            "lock_path": sidecar.get("lock_path"),
            "recovered_from_sidecar": True,
        }
    )
    atomic_json(directory / "job.json", recovered)
    return recovered


def read_meta(root: Path, job_id: str) -> tuple[Path, dict]:
    directory = job_path(root, job_id)
    metadata = directory / "job.json"
    if not metadata.is_file():
        raise FileNotFoundError(f"job not found: {job_id}")
    meta = json.loads(metadata.read_text(encoding="utf-8"))
    return directory, recover_meta(directory, meta)


def run_registry_path(root: Path, run_id: str) -> Path:
    return run_registry.registry_path(root, run_id)


def registry_session(root: Path, run_id: str, provider: str) -> str | None:
    # 충돌을 여기서 삼키지 않는다. 남의 session을 조용히 물려주는 것보다 세우는 편이 낫다.
    return run_registry.session_for(root, run_id, provider)


def session_activity_paths(meta: dict) -> list[Path]:
    session_id = meta.get("session_id")
    if not session_id:
        return []
    home = Path.home()
    provider = meta["provider"]
    patterns = {
        "claude": (home / ".claude" / "projects", f"**/{session_id}.jsonl"),
        "grok": (home / ".grok" / "sessions", f"**/{session_id}/updates.jsonl"),
        "codex": (home / ".codex" / "sessions", f"**/*{session_id}*.jsonl"),
    }
    base_pattern = patterns.get(provider)
    if not base_pattern or not base_pattern[0].exists():
        return []
    return list(base_pattern[0].glob(base_pattern[1]))


def activity_payload(directory: Path, meta: dict) -> dict:
    """Read-only activity fields shared by status, wait, and watch."""
    if not {"provider", "started_epoch", "check_after_seconds"}.issubset(meta):
        return {}
    started_epoch = float(meta["started_epoch"])
    elapsed = max(0.0, time.time() - started_epoch)
    raw_result = directory / "result.pending.json"
    activity = progress.read(directory) if meta["provider"] == "codex" else {}
    effective_session = activity.get("session_id") or meta.get("session_id")
    session_paths = session_activity_paths({**meta, "session_id": effective_session})
    activity_paths = [raw_result, directory / "launcher.stderr", *session_paths]
    last_activity = max(latest_mtime(activity_paths, started_epoch), activity.get("last_event_at", 0))
    idle = max(0.0, time.time() - last_activity)
    check_after = float(meta["check_after_seconds"])
    needs_attention = elapsed >= check_after and idle >= check_after
    attention_basis = "provider_event_stream" if activity else "session_activity" if session_paths else "no_session_signal"
    return {
        "provider": meta["provider"], "run_id": meta.get("run_id"), "profile": meta.get("profile"),
        "session_id": effective_session, "pid": meta.get("pid"), "pgid": meta.get("pgid"),
        "duration_seconds": round(elapsed, 3), "idle_seconds": round(idle, 3),
        "check_after_seconds": check_after, "needs_attention": needs_attention,
        "attention_basis": attention_basis, "attention_signal": "strong" if activity or session_paths else "weak",
        "event_count": activity.get("event_count", 0), "last_event_type": activity.get("last_event_type"),
        "last_event_at": activity.get("last_event_at"),
        "progress_degraded": activity.get("observation_degraded", False),
    }


def status_payload(root: Path, job_id: str) -> dict:
    directory, meta = read_meta(root, job_id)
    profile = meta.get("profile", "review")
    result_path = directory / "result.json"
    raw_result = directory / "result.pending.json"
    canceled = directory / "canceled.json"
    started_epoch = float(meta["started_epoch"])
    elapsed = max(0.0, time.time() - started_epoch)

    if canceled.is_file():
        payload = json.loads(canceled.read_text(encoding="utf-8"))
        payload.update(
            {
                "schema_version": "1.0",
                "job_id": job_id,
                "provider": meta["provider"],
                "run_id": meta["run_id"],
                "profile": profile,
            }
        )
        return payload

    if meta.get("state") == "spawn_failed":
        return {
            "schema_version": "1.0",
            "status": "failed",
            "job_id": job_id,
            "provider": meta["provider"],
            "run_id": meta["run_id"],
            "profile": profile,
            "duration_seconds": round(elapsed, 3),
            "error": meta.get("spawn_error", "worker spawn failed"),
            "auto_canceled": False,
        }

    ready_result = parse_result(raw_result)
    if ready_result is not None:
        if not result_path.exists():
            atomic_json(result_path, ready_result)
        return {
            "schema_version": "1.0",
            "status": "completed" if ready_result.get("status") == "ok" else "failed",
            "job_id": job_id,
            "provider": meta["provider"],
            "run_id": meta["run_id"],
            "profile": profile,
            "worker_status": ready_result.get("status"),
            "exit_code": ready_result.get("exit_code"),
            "duration_seconds": ready_result.get("duration_seconds", round(elapsed, 3)),
            "collect_command": f"{ROOT / 'worker_job.sh'} collect {job_id}",
            "identity_verified": identity_status(meta) in {"verified", "dead"},
            "auto_canceled": False,
        }

    identity = identity_status(meta)
    if identity == "starting":
        grace = float(meta.get("spawn_grace_seconds", 10))
        if elapsed <= grace:
            return {
                "schema_version": "1.0",
                "status": "starting",
                "job_id": job_id,
                "provider": meta["provider"],
                "run_id": meta["run_id"],
                "profile": profile,
                "duration_seconds": round(elapsed, 3),
                "identity_verified": False,
                "auto_canceled": False,
            }
        return {
            "schema_version": "1.0",
            "status": "failed",
            "job_id": job_id,
            "provider": meta["provider"],
            "run_id": meta["run_id"],
            "profile": profile,
            "duration_seconds": round(elapsed, 3),
            "error": "launcher did not register before spawn grace expired",
            "identity_verified": False,
            "auto_canceled": False,
        }

    if identity == "mismatch":
        return {
            "schema_version": "1.0",
            "status": "failed",
            "job_id": job_id,
            "provider": meta["provider"],
            "run_id": meta["run_id"],
            "profile": profile,
            "duration_seconds": round(elapsed, 3),
            "error": "stale PID metadata; launcher identity mismatch",
            "identity_verified": False,
            "cancel_safe": False,
            "auto_canceled": False,
        }

    if identity == "dead":
        result = parse_result(raw_result)
        if result is not None:
            if not result_path.exists():
                atomic_json(result_path, result)
            return {
                "schema_version": "1.0",
                "status": "completed" if result.get("status") == "ok" else "failed",
                "job_id": job_id,
                "provider": meta["provider"],
                "run_id": meta["run_id"],
                "profile": profile,
                "worker_status": result.get("status"),
                "exit_code": result.get("exit_code"),
                "duration_seconds": result.get("duration_seconds", round(elapsed, 3)),
                "collect_command": f"{ROOT / 'worker_job.sh'} collect {job_id}",
                "identity_verified": True,
                "auto_canceled": False,
            }
        stderr_path = directory / "launcher.stderr"
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")[-4096:] if stderr_path.exists() else ""
        return {
            "schema_version": "1.0",
            "status": "failed",
            "job_id": job_id,
            "provider": meta["provider"],
            "run_id": meta["run_id"],
            "profile": profile,
            "duration_seconds": round(elapsed, 3),
            "error": "worker process exited without a valid result envelope",
            "launcher_stderr": stderr,
            "identity_verified": True,
            "auto_canceled": False,
        }

    activity = activity_payload(directory, meta)
    return {
        "schema_version": "1.0", "status": "running", "job_id": job_id, **activity,
        "identity_verified": identity == "verified", "identity_status": identity,
        "auto_canceled": False,
        "action": "keep_waiting_or_explicitly_cancel" if activity["needs_attention"] else "keep_waiting",
        "status_command": f"{ROOT / 'worker_job.sh'} status {job_id}",
        "cancel_command": f"{ROOT / 'worker_job.sh'} cancel {job_id}",
    }


def broker_start_payload(args: argparse.Namespace, brief: Path, target: Path) -> dict:
    """Describe this start request for the broker, which re-validates every field."""
    payload: dict = {
        "action": "start",
        "provider": args.provider,
        "host": args.host,
        "profile": args.profile,
        "mode": args.mode,
        "brief": str(brief),
        "target": str(target),
    }
    if args.run_id:
        payload["run_id"] = args.run_id
    if args.job_id:
        payload["job_id"] = args.job_id
    if args.session_id:
        payload["session_id"] = args.session_id
    if args.model:
        payload["model"] = args.model
    if args.effort:
        payload["effort"] = args.effort
    if args.media_kind:
        payload["media_kind"] = args.media_kind
    if args.output_dir:
        payload["output_dir"] = str(args.output_dir.expanduser().resolve())
    if args.aspect_ratio:
        payload["aspect_ratio"] = args.aspect_ratio
    if args.duration:
        payload["duration"] = args.duration
    if args.rehydrate_file:
        payload["rehydrate_file"] = str(args.rehydrate_file.expanduser().resolve())
    if args.check_after is not None:
        payload["check_after"] = args.check_after
    # A sandboxed host reaches its provider through the broker, so a label that
    # stops here is a label the brokered job never sees.
    if args.mission:
        payload["mission"] = args.mission
    if args.role:
        payload["role"] = args.role
    if args.round:
        payload["round"] = args.round
    return payload


def start_job(args: argparse.Namespace, config: dict, root: Path) -> int:
    # Validated before anything is created: an invalid label must not leave an
    # orphan job directory behind, and must not reach the broker either.
    if args.mission is not None and not mission_log.valid_mission_id(args.mission):
        print_json({"status": "invalid_mission_id", "exit_code": 64, "mission_id": args.mission,
                    "hint": "mission ID는 [A-Za-z0-9][A-Za-z0-9_.-]{0,63} 이어야 한다. 치환하지 않는다."})
        return 64
    if args.role is not None and not mission_log.valid_role(args.role):
        print_json({"status": "invalid_role", "exit_code": 64, "role": args.role,
                    "hint": f"role은 1~{mission_log.ROLE_MAX}자이고 제어문자를 포함할 수 없다."})
        return 64
    if args.round is not None and not mission_log.valid_round(args.round):
        print_json({"status": "invalid_round", "exit_code": 64, "round": args.round,
                    "hint": f"round는 1~{mission_log.ROUND_MAX} 사이의 정수다."})
        return 64

    brief = args.brief.expanduser().resolve()
    target = args.target.expanduser().resolve()
    if not brief.is_file() or not target.is_dir():
        print_json({"status": "invalid_input", "exit_code": 66, "brief": str(brief), "target": str(target)})
        return 66

    blocked = billing.refusal(args.provider, target)
    if blocked:
        print_json(blocked)
        return 78

    provider_config = config.get("providers", {}).get(args.provider, {})
    if args.profile not in provider_config.get("profiles", []):
        print_json(
            {
                "status": "unsupported_profile",
                "exit_code": 64,
                "provider": args.provider,
                "profile": args.profile,
                "supported_profiles": provider_config.get("profiles", []),
            }
        )
        return 64
    effort_error = effort_registry.validation_error(config, args.provider, args.effort)
    if effort_error:
        status, hint = effort_error
        print_json({
            "status": status,
            "exit_code": 64,
            "provider": args.provider,
            "effort": args.effort,
            "supported_efforts": effort_registry.values(config, args.provider),
            "effort_providers": effort_registry.providers(config),
            "hint": hint,
        })
        return 64
    media_options_present = any(
        value is not None
        for value in (args.media_kind, args.output_dir, args.aspect_ratio, args.duration)
    )
    if args.profile == "media":
        if args.mode != "fresh" or not args.media_kind or not args.output_dir:
            print_json(
                {
                    "status": "invalid_media_input",
                    "exit_code": 64,
                    "provider": args.provider,
                    "mode": args.mode,
                    "error": "media requires mode=fresh, --media-kind, and --output-dir",
                }
            )
            return 64
        media_error = media_registry.validation_error(
            config, args.provider, args.media_kind, args.aspect_ratio, args.duration
        )
        if media_error:
            status, hint = media_error
            print_json(
                {
                    "status": status,
                    "exit_code": 64,
                    "provider": args.provider,
                    "media_kind": args.media_kind,
                    "supported_kinds": media_registry.kinds(config, args.provider),
                    "media_providers": media_registry.providers(config),
                    "hint": hint,
                }
            )
            return 64
        if args.media_kind == "video":
            privacy = video_privacy_preflight()
            if not privacy["ready"]:
                print_json(
                    {
                        "schema_version": "1.0",
                        "status": "zdr_output_required",
                        "exit_code": 78,
                        "retryable": False,
                        "provider": args.provider,
                        "profile": args.profile,
                        "media_kind": args.media_kind,
                        "privacy_scope": privacy["privacy_scope"],
                        "private_output_configured": privacy["private_output_configured"],
                        "config_source": privacy["config_source"],
                        "auto_canceled": False,
                        "hint": (
                            "Configure tools.zdr_video_output_s3 with an HTTPS endpoint "
                            "in a mode-0600 Grok config. No job was started."
                        ),
                    }
                )
                return 78
    elif media_options_present:
        print_json(
            {
                "status": "invalid_media_input",
                "exit_code": 64,
                "error": "media options require --profile media",
            }
        )
        return 64

    route = route_for(config, args.host, args.provider, args.profile, args.mode)

    if route == "in_process" and not args.allow_self_delegation:
        print_json(
            {
                "schema_version": "1.0",
                "status": "self_delegation",
                "exit_code": 64,
                "provider": args.provider,
                "host": args.host,
                "route": route,
                "auto_canceled": False,
                "hint": (
                    "The host and the provider are the same agent. Answer in-process "
                    "instead of starting a job, or pass --allow-self-delegation for a "
                    "deliberate independent second opinion."
                ),
            }
        )
        return 64

    if route in {"broker", "requires_host_escalation"} and not args.host_escalated:
        broker_error = None
        if route == "broker":
            try:
                brokered = broker_client.request(
                    state_dir(config), broker_start_payload(args, brief, target)
                )
            except broker_client.BrokerUnavailable as exc:
                brokered = None
                broker_error = str(exc)
            if brokered is not None:
                print_json(brokered)
                status = brokered.get("status")
                if status in {"started", "ok"}:
                    return 0
                return int(brokered.get("exit_code") or 1)
        print_json(
            {
                "schema_version": "1.0",
                "status": "needs_host_escalation",
                "exit_code": 77,
                "provider": args.provider,
                "host": args.host,
                "route": route,
                "requires_host_escalation": True,
                "broker_available": False,
                "broker_error": broker_error,
                "auto_canceled": False,
                "hint": (
                    "This host cannot spawn the provider itself. Start the escalation "
                    "broker with broker_service.sh install, or run the same start "
                    "command outside the host sandbox and add --host-escalated."
                ),
            }
        )
        return 77

    job_id = safe_job_id(
        args.job_id or f"{args.provider}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    )
    directory = job_path(root, job_id)
    try:
        directory.mkdir(parents=False, exist_ok=False, mode=0o700)
        directory.chmod(0o700)
    except FileExistsError:
        print_json({"status": "conflict", "exit_code": 75, "error": f"job already exists: {job_id}"})
        return 75

    durable_brief = directory / "brief.md"
    shutil.copyfile(brief, durable_brief)
    durable_brief.chmod(0o600)
    run_id = args.run_id or job_id
    session_id = args.session_id
    session_arg = args.session_id
    if args.mode == "fresh" and args.provider in {"claude", "grok"} and not session_id:
        session_id = str(uuid.uuid4())
        session_arg = session_id
    elif args.mode == "resume" and not session_id:
        try:
            session_id = registry_session(root, run_id, args.provider)
        except run_registry.RunIdCollision as exc:
            print_json({"status": "run_id_collision", "exit_code": 64, "run_id": run_id,
                        "conflicting_run_id": exc.stored, "registry_file": exc.path.name})
            return 64

    command = [
        sys.executable,
        str(ROOT / "call_worker.py"),
        args.provider,
        str(durable_brief),
        "--host",
        args.host,
        "--target",
        str(target),
        "--mode",
        args.mode,
        "--profile",
        args.profile,
        "--run-id",
        run_id,
        "--timeout",
        "0",
    ]
    if args.model:
        command += ["--model", args.model]
    if args.effort:
        command += ["--effort", args.effort]
    if args.media_kind:
        command += ["--media-kind", args.media_kind]
    if args.output_dir:
        command += ["--output-dir", str(args.output_dir.expanduser().resolve())]
    if args.aspect_ratio:
        command += ["--aspect-ratio", args.aspect_ratio]
    if args.duration:
        command += ["--duration", str(args.duration)]
    if session_arg:
        command += ["--session-id", session_arg]
    if args.rehydrate_file:
        command += ["--rehydrate-file", str(args.rehydrate_file.expanduser().resolve())]
    if args.host_escalated:
        command.append("--host-escalated")

    check_after = (
        args.check_after if args.check_after is not None else float(config["defaults"].get("check_after_seconds", 600))
    )
    started_epoch = time.time()
    launcher_token = uuid.uuid4().hex
    meta = {
        "schema_version": "1.0",
        "state": "spawning",
        "job_id": job_id,
        "provider": args.provider,
        "host": args.host,
        "run_id": run_id,
        "session_id": session_id,
        "mode": args.mode,
        "profile": args.profile,
        "target": str(target),
        "mission_id": args.mission,
        "role": args.role,
        "round": args.round,
        "model": args.model,
        "reasoning_effort": args.effort,
        "media_kind": args.media_kind,
        "output_dir": str(args.output_dir.expanduser().resolve()) if args.output_dir else None,
        "aspect_ratio": args.aspect_ratio,
        "duration": args.duration,
        "pid": None,
        "pgid": None,
        "launcher_token": launcher_token,
        "sidecar_path": str(directory / "launcher.json"),
        "lock_path": str(directory / "launcher.lock"),
        "started_at": now_iso(),
        "started_epoch": started_epoch,
        "spawn_grace_seconds": 10,
        "check_after_seconds": check_after,
        "hard_timeout_seconds": None,
        "auto_cancel": False,
    }
    atomic_json(directory / "job.json", meta)

    result_handle = private_text_handle(directory / "result.pending.json")
    stderr_handle = private_text_handle(directory / "launcher.stderr")
    process: subprocess.Popen | None = None
    try:
        env = os.environ.copy()
        env["CROSSCREW_SUPERVISED"] = "1"
        env["CROSSCREW_LAUNCHER_SIDECAR"] = str(directory / "launcher.json")
        env["CROSSCREW_LAUNCHER_TOKEN"] = launcher_token
        env.pop("CROSSCREW_PROGRESS_FILE", None)
        if args.provider == "codex":
            env["CROSSCREW_PROGRESS_FILE"] = str(directory / "progress.json")
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=result_handle,
            stderr=stderr_handle,
            cwd=target,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        meta.update({"state": "running", "pid": process.pid, "pgid": os.getpgid(process.pid)})
        atomic_json(directory / "job.json", meta)
        sidecar_deadline = time.time() + 1
        while time.time() < sidecar_deadline and not (directory / "launcher.json").exists() and process_alive(process.pid):
            time.sleep(0.01)
    except Exception as exc:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        meta.update({"state": "spawn_failed", "spawn_error": str(exc)})
        atomic_json(directory / "job.json", meta)
        print_json(
            {
                "schema_version": "1.0",
                "status": "spawn_failed",
                "exit_code": 71,
                "job_id": job_id,
                "provider": args.provider,
                "error": str(exc),
                "auto_canceled": False,
            }
        )
        return 71
    finally:
        result_handle.close()
        stderr_handle.close()

    # Observation must not break execution (spec 4.1): the job is already
    # spawned, so a failed append is reported and nothing else.
    event_logged = True
    if args.mission:
        event_logged = mission_log.append(root, args.mission, {
            "e": "delegation.started", "mission_id": args.mission, "job_id": job_id,
            "provider": args.provider, "host": args.host, "profile": args.profile,
            "role": args.role, "round": args.round, "target": str(target)})

    print_json(
        {
            "schema_version": "1.0",
            "status": "running",
            "job_id": job_id,
            "provider": args.provider,
            "run_id": run_id,
            "profile": args.profile,
            "target": str(target),
            "mission_id": args.mission,
            "role": args.role,
            "round": args.round,
            "event_log_degraded": bool(args.mission) and not event_logged,
            "model": args.model,
            "reasoning_effort": args.effort,
            "media_kind": args.media_kind,
            "output_dir": str(args.output_dir.expanduser().resolve()) if args.output_dir else None,
            "session_id": session_id,
            "pid": process.pid,
            "pgid": meta["pgid"],
            "identity_verified": identity_status(meta) == "verified",
            "check_after_seconds": check_after,
            "hard_timeout_seconds": None,
            "auto_canceled": False,
            "status_command": f"{ROOT / 'worker_job.sh'} status {job_id}",
            "collect_command": f"{ROOT / 'worker_job.sh'} collect {job_id}",
            "cancel_command": f"{ROOT / 'worker_job.sh'} cancel {job_id}",
        }
    )
    return 0


def cancel_job(root: Path, job_id: str) -> int:
    status = status_payload(root, job_id)
    if status["status"] == "canceled":
        print_json(status)
        return 0
    if status.get("cancel_safe") is False:
        payload = {
            "schema_version": "1.0",
            "status": "cancel_unverified",
            "exit_code": 75,
            "job_id": job_id,
            "provider": status.get("provider"),
            "identity_status": "mismatch",
            "auto_canceled": False,
            "reason": "launcher identity mismatch; refusing to signal",
        }
        print_json(payload)
        return 75
    if status["status"] not in ACTIVE_STATUSES:
        payload = {
            "schema_version": "1.0",
            "status": "already_completed",
            "exit_code": 0,
            "job_id": job_id,
            "provider": status.get("provider"),
            "prior_status": status["status"],
            "auto_canceled": False,
        }
        print_json(payload)
        return 0

    directory, meta = read_meta(root, job_id)
    identity = identity_status(meta)
    if identity != "verified":
        payload = {
            "schema_version": "1.0",
            "status": "cancel_unverified",
            "exit_code": 75,
            "job_id": job_id,
            "provider": meta["provider"],
            "identity_status": identity,
            "auto_canceled": False,
            "reason": "launcher identity could not be verified; refusing to signal",
        }
        print_json(payload)
        return 75

    pgid = int(meta["pgid"])
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.time() + 3
    while time.time() < deadline and process_group_alive(pgid):
        time.sleep(0.1)
    if process_group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.time() + 2
        while time.time() < deadline and process_group_alive(pgid):
            time.sleep(0.1)
    if process_group_alive(pgid):
        payload = {
            "schema_version": "1.0",
            "status": "cancel_failed",
            "exit_code": 1,
            "job_id": job_id,
            "provider": meta["provider"],
            "remaining_pgid": pgid,
            "auto_canceled": False,
        }
        print_json(payload)
        return 1

    payload = {
        "status": "canceled",
        "exit_code": 130,
        "canceled_at": now_iso(),
        "auto_canceled": False,
        "reason": "explicit_cancel",
    }
    atomic_json(directory / "canceled.json", payload)
    orphan_grants = cleanup_orphan_projects() if meta["provider"] == "agy" else []
    print_json(
        {
            "schema_version": "1.0",
            "job_id": job_id,
            "provider": meta["provider"],
            "agy_orphan_grants_removed": orphan_grants,
            **payload,
        }
    )
    return 0


def purge_jobs(root: Path, older_than_days: float) -> int:
    cutoff = time.time() - max(0.0, older_than_days) * 86400
    removed: list[str] = []
    skipped_active: list[str] = []
    for directory in sorted((root / "jobs").iterdir()):
        if not directory.is_dir():
            continue
        try:
            status = status_payload(root, directory.name)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            continue
        if status["status"] in ACTIVE_STATUSES:
            skipped_active.append(directory.name)
            continue
        try:
            age_anchor = max(path.stat().st_mtime for path in directory.iterdir())
        except (OSError, ValueError):
            continue
        if age_anchor <= cutoff:
            shutil.rmtree(directory)
            removed.append(directory.name)
    orphan_grants = cleanup_orphan_projects()
    print_json(
        {
            "schema_version": "1.0",
            "status": "purged",
            "exit_code": 0,
            "older_than_days": older_than_days,
            "removed_jobs": removed,
            "skipped_active_jobs": skipped_active,
            "agy_orphan_grants_removed": orphan_grants,
        }
    )
    return 0


DISPOSITIONS = ("accepted", "rejected", "repaired", "closed_with_gaps")
# lifecycle 어휘의 정본은 projection이다. 여기서 다시 적으면 둘이 어긋난다.
TERMINAL = projection.TERMINAL


def _mission_jobs(root: Path, mission_id: str) -> dict[str, dict]:
    """이 mission에 속한 job의 projection. 부작용 없다."""
    return {job["job_id"]: job for job in projection.read_jobs(root)
            if job.get("mission_id") == mission_id}


def _refuse(status: str, **extra) -> int:
    print_json({"status": status, "exit_code": 64, **extra})
    return 64


def mission_command(args, root: Path) -> int:
    mission_id = args.mission_id
    if not mission_log.valid_mission_id(mission_id):
        return _refuse("invalid_mission_id", mission_id=mission_id)
    events = mission_log.read_events(root, mission_id)
    opened = any(event.get("e") == "mission.opened" for event in events)

    if args.mission_command == "open":
        target = args.target.expanduser().resolve()
        if not target.is_dir():
            print_json({"status": "invalid_input", "exit_code": 66,
                        "error": f"target not a directory: {target}"})
            return 66
        logged = mission_log.append(root, mission_id, {
            "e": "mission.opened", "mission_id": mission_id,
            "goal": args.goal, "target": str(target)})
        print_json({"status": "opened", "mission_id": mission_id,
                    "event_log_degraded": not logged})
        return 0

    if not opened:
        return _refuse("mission_not_open", mission_id=mission_id,
                       hint="mission open 을 먼저 실행한다.")

    if args.mission_command == "dispose":
        chosen = [name for name, flag in (
            ("accepted", args.accepted), ("rejected", args.rejected),
            ("repaired", args.repaired), ("closed_with_gaps", args.closed_with_gaps),
        ) if flag]
        if len(chosen) != 1:
            return _refuse("invalid_input", error="판정 플래그를 정확히 하나 준다",
                           dispositions=list(DISPOSITIONS))
        jobs = _mission_jobs(root, mission_id)
        job = jobs.get(args.job_id)
        if job is None:
            # mission에 없거나 아예 없는 job인지 구분해서 알려준다.
            exists = any(entry["job_id"] == args.job_id for entry in projection.read_jobs(root))
            return _refuse("job_mission_mismatch" if exists else "job_not_found",
                           mission_id=mission_id, job_id=args.job_id)
        if job["lifecycle_status"] not in TERMINAL:
            return _refuse("job_not_terminal", job_id=args.job_id,
                           lifecycle_status=job["lifecycle_status"])
        if any(event.get("e") == "disposition.declared" and event.get("job_id") == args.job_id
               for event in events):
            return _refuse("already_disposed", job_id=args.job_id)
        logged = mission_log.append(root, mission_id, {
            "e": "disposition.declared", "mission_id": mission_id, "job_id": args.job_id,
            "disposition": chosen[0], "note": args.note,
            "changed_host_conclusion": bool(args.changed_conclusion)})
        print_json({"status": "disposed", "mission_id": mission_id, "job_id": args.job_id,
                    "disposition": chosen[0], "event_log_degraded": not logged})
        return 0

    # close
    disposed = {event.get("job_id") for event in events
                if event.get("e") == "disposition.declared"}
    mission_jobs = _mission_jobs(root, mission_id)
    # 실행 중인 job을 두고 깨끗하게 닫으면, 그 job이 끝난 뒤 미처분으로 남는데
    # mission은 이미 "결손 없음"을 주장하고 있다. undisposed 검사는 terminal job만
    # 보므로 이 구멍을 못 막는다. 멈춘 워커를 영원히 기다리지 않도록
    # --closed-with-gaps 로 결손을 적고 닫는 길은 열어 둔다.
    in_flight = sorted(job_id for job_id, job in mission_jobs.items()
                       if job["lifecycle_status"] not in TERMINAL)
    if in_flight and not args.closed_with_gaps:
        return _refuse("jobs_in_flight", mission_id=mission_id, in_flight=in_flight,
                       hint="끝나기를 기다리거나 --closed-with-gaps --unresolved 로 닫는다.")
    undisposed = sorted(job_id for job_id, job in mission_jobs.items()
                        if job["lifecycle_status"] in TERMINAL and job_id not in disposed)
    if undisposed and not args.closed_with_gaps:
        # 미처분을 남기고 깨끗하게 닫으면 화면이 완료로 위장한다.
        return _refuse("undisposed_remaining", mission_id=mission_id, undisposed=undisposed,
                       hint="각 job을 dispose 하거나 --closed-with-gaps 로 닫는다.")
    if args.closed_with_gaps and not args.unresolved:
        return _refuse("invalid_input",
                       error="--closed-with-gaps 는 --unresolved 를 최소 하나 요구한다")
    logged = mission_log.append(root, mission_id, {
        "e": "mission.closed", "mission_id": mission_id,
        "closed_with_gaps": bool(args.closed_with_gaps),
        "unresolved": list(args.unresolved)})
    print_json({"status": "closed", "mission_id": mission_id,
                "unresolved": list(args.unresolved), "event_log_degraded": not logged})
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    subs = value.add_subparsers(dest="command", required=True)

    start = subs.add_parser("start")
    start.add_argument("provider", choices=PROVIDERS)
    start.add_argument("brief", type=Path)
    start.add_argument("--host", choices=HOSTS, default="shell")
    start.add_argument("--target", type=Path, default=Path.cwd())
    start.add_argument("--mode", choices=("oneshot", "fresh", "resume"), default="oneshot")
    start.add_argument("--profile", choices=("review", "work", "research", "media"), default="review")
    start.add_argument("--model")
    # Validated by the dispatcher against the registry, not by argparse here.
    start.add_argument("--effort")
    # Bounds live in mission_log, checked in start_job, so the broker and the
    # supervisor reject on identical grounds instead of two argparse copies.
    start.add_argument("--mission")
    start.add_argument("--role")
    start.add_argument("--round", type=int)
    start.add_argument("--media-kind", choices=("image", "video"))
    start.add_argument("--output-dir", type=Path)
    start.add_argument(
        "--aspect-ratio",
        choices=("auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "2:1", "1:2", "19.5:9", "9:19.5", "20:9", "9:20"),
    )
    start.add_argument("--duration", type=int, choices=(6, 10))
    start.add_argument("--session-id")
    start.add_argument("--run-id")
    start.add_argument("--job-id")
    start.add_argument("--check-after", type=float)
    start.add_argument("--rehydrate-file", type=Path)
    start.add_argument("--host-escalated", action="store_true")
    start.add_argument(
        "--allow-self-delegation",
        action="store_true",
        help="permit a host to start a job against its own provider",
    )

    status = subs.add_parser("status")
    status.add_argument("job_id")
    collect = subs.add_parser("collect")
    collect.add_argument("job_id")
    cancel = subs.add_parser("cancel")
    cancel.add_argument("job_id")
    watch = subs.add_parser("watch")
    watch.add_argument("job_id")
    watch.add_argument("--interval", type=float, default=60)
    waiting = subs.add_parser("wait", help="상태를 변경하지 않고 종료와 결과 참조를 기다린다")
    waiting.add_argument("job_id")
    waiting.add_argument("--interval", type=float, default=2)
    waiting.add_argument("--timeout", type=float, default=0, help="대기만 종료한다. 0은 무제한")
    listing = subs.add_parser("list")
    listing.add_argument("--running", action="store_true", help="아직 끝나지 않은 job만")
    listing.add_argument("--mission", help="이 mission의 job만")
    listing.add_argument("--limit", type=int, default=30)

    mission = subs.add_parser("mission")
    mission_subs = mission.add_subparsers(dest="mission_command", required=True)

    m_open = mission_subs.add_parser("open")
    m_open.add_argument("mission_id")
    m_open.add_argument("--goal", required=True)
    m_open.add_argument("--target", type=Path, required=True)

    m_dispose = mission_subs.add_parser("dispose")
    m_dispose.add_argument("mission_id")
    m_dispose.add_argument("job_id")
    m_dispose.add_argument("--accepted", action="store_true")
    m_dispose.add_argument("--rejected", action="store_true")
    m_dispose.add_argument("--repaired", action="store_true")
    m_dispose.add_argument("--closed-with-gaps", action="store_true")
    m_dispose.add_argument("--note")
    m_dispose.add_argument("--changed-conclusion", action="store_true")

    m_close = mission_subs.add_parser("close")
    m_close.add_argument("mission_id")
    m_close.add_argument("--closed-with-gaps", action="store_true")
    m_close.add_argument("--unresolved", action="append", default=[])

    purge = subs.add_parser("purge")
    purge.add_argument("--older-than-days", type=float, default=30)
    return value


def main() -> None:
    args = parser().parse_args()
    config = load_config()
    if args.command in {"wait", "watch"}:
        raise SystemExit(job_wait.run(projection.resolve_state_dir(config, ROOT), args.job_id,
                                      args.interval, getattr(args, "timeout", 0),
                                      watch=args.command == "watch", activity_reader=activity_payload))
    if args.command == "list":
        # 무쓰기 경로. state_dir()은 디렉터리를 만들고 chmod하므로 쓰지 않는다.
        full = projection.snapshot(projection.resolve_state_dir(config, ROOT))
        jobs, missions, rail = full["jobs"], full["missions"], full["rail"]
        if args.mission:
            # --mission은 범위를 고른다. 그래서 레일도 그 범위에서 다시 센다 —
            # 한 건을 보면서 전체 레일 숫자를 붙이면 남의 빚이 이 건의 빚으로 보인다.
            jobs = [job for job in jobs if job.get("mission_id") == args.mission]
            missions = [entry for entry in missions
                        if entry["mission_id"] == args.mission]
            rail = projection.rail(jobs, missions)
        if args.running:
            # --running과 --limit은 표시할 부분집합이다. 여기서 레일을 다시 세면
            # undisposed가 구조적으로 0이 되어 화면이 없는 안전을 주장한다.
            unfinished = {"starting", "running", "state_unconfirmed"}
            jobs = [job for job in jobs if job["lifecycle_status"] in unfinished]
        print_json({"schema_version": projection.SCHEMA_VERSION, "count": len(jobs),
                    "rail": rail, "missions": missions, "jobs": jobs[: args.limit]})
        return
    root = state_dir(config)
    try:
        if args.command == "start":
            raise SystemExit(start_job(args, config, root))
        if args.command == "mission":
            raise SystemExit(mission_command(args, root))
        if args.command == "status":
            print_json(status_payload(root, args.job_id))
            return
        if args.command == "collect":
            directory, _ = read_meta(root, args.job_id)
            status = status_payload(root, args.job_id)
            if status["status"] in ACTIVE_STATUSES:
                print_json(status)
                raise SystemExit(75)
            if status["status"] == "canceled":
                print_json(status)
                raise SystemExit(130)
            result = parse_result(directory / "result.json") or parse_result(directory / "result.pending.json")
            print_json(result or status)
            raise SystemExit(0 if result and result.get("status") == "ok" else 1)
        if args.command == "cancel":
            raise SystemExit(cancel_job(root, args.job_id))
        if args.command == "purge":
            raise SystemExit(purge_jobs(root, args.older_than_days))
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print_json({"status": "invalid_input", "exit_code": 66, "error": str(exc)})
        raise SystemExit(66)


if __name__ == "__main__":
    main()
