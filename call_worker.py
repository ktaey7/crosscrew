#!/usr/bin/env python3
"""Host-neutral adapter for Claude, Codex, Grok, and agy workers."""

from __future__ import annotations

import argparse
import contextlib
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
import tempfile
import time
import uuid

from agy_grants import AgyProjectGrant, create_work_project, release_work_project
import billing
import broker_client
import effort_registry
from grok_media_privacy import video_privacy_preflight
from media_artifacts import ArtifactError, collect_media_artifacts
import media_registry
import readonly_sandbox
import run_registry
import progress
import stream_process

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 on the host; model metadata is optional.
    tomllib = None


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CROSSCREW_BACKENDS_CONFIG", ROOT / "backends.json"))
ALLOWED_COMMANDS = {"claude", "codex", "grok", "agy"}
ALLOWED_HOSTS = ("claude", "codex", "grok", "agy", "shell")
# Most restrictive first: the effective route is the strictest one any of the
# base/profile/mode matrices asks for.
ROUTE_PRECEDENCE = (
    "requires_host_escalation",
    "broker",
    "in_process",
    "companion_preferred",
    "direct",
)
LAUNCHER_LOCK_HANDLE = None
REVIEW_WORKER_GUARD = """You are an external analysis and verification worker.
You may use the available tools freely to inspect files, run diagnostics, tests, builds, read-only database queries, log checks, and web research needed for the task.
Do not intentionally edit, delete, move, or overwrite source, configuration, or user files. Do not stage, commit, push, install or update dependencies, send messages, or perform other external writes.
When a diagnostic inherently creates disposable caches or build artifacts, prefer a temporary directory when supported and disclose any artifact left behind.
Return analysis and evidence to the host AI; the host performs requested modifications separately."""

RESEARCH_WORKER_GUARD = """You are an external research worker.
Research the task thoroughly using web, documentation, and read-only local inspection when relevant. Do not intentionally edit, delete, move, or overwrite source, configuration, or user files.
Do not stage, commit, push, install or update dependencies, deploy, send messages, or perform other external writes. Return findings, sources, uncertainties, and useful next actions to the host AI."""

WORK_WORKER_GUARD = """You are an external implementation worker.
Complete the requested task end-to-end. You may freely inspect, create, edit, move, and delete files inside the target working directory; run diagnostics, tests, builds, and formatters; and install project-local dependencies when the task requires them.
Stay within the target directory and the user's brief. Do not modify files outside the target, change credentials, install system-wide software, commit, push, deploy, or send external messages unless the brief explicitly authorizes that exact action.
Do not broaden the task on your own. Report the files changed, commands run, verification results, and any remaining risks."""

MEDIA_WORKER_GUARD = """You are a narrowly scoped Grok Imagine media worker.
Generate exactly one requested final media artifact using only the Imagine tools exposed for this session. Do not use shell, file-editing, browser, web-search, deployment, subagent, memory, or external API tools.
Do not inspect or modify project files. Do not handle patient-identifying data, private-tagged material, real-person likeness references, file attachments, filesystem image references, or data-image URLs in this profile.
Do not use API keys or call xAI APIs directly. The authenticated Grok Build Imagine tool is the only permitted generation path.
Return a short factual completion note. The host will independently locate, validate, hash, and copy the generated artifact; do not claim success unless the Imagine tool completed."""

MEDIA_REFERENCE_PATTERNS = (
    re.compile(r"(?i)\bdata:image/"),
    re.compile(r"(?i)\bfile://"),
    re.compile(r"(?i)(?:^|[\s\"'(])(?:/|~\/)[^\s\"')]+\.(?:jpe?g|png|webp|gif|heic)\b"),
    re.compile(r"(?i)\[(?:image|video|file)\s*#?\d+\]"),
)
PHI_PATTERNS = (
    re.compile(r"\b\d{6}-?[1-4]\d{6}\b"),
    re.compile(r"(?i)(?:환자\s*(?:번호|id|성명|이름)|병록번호|의무기록번호|patient\s*(?:id|name))\s*[:=]\s*\S+"),
    re.compile(r"(?i)<private>"),
)


def validate_media_brief(text: str) -> str | None:
    if any(pattern.search(text) for pattern in MEDIA_REFERENCE_PATTERNS):
        return "reference_media_blocked"
    if any(pattern.search(text) for pattern in PHI_PATTERNS):
        return "phi_blocked"
    return None


CODEX_MEDIA_WORKER_GUARD = """You are a narrowly scoped image generation worker.
Generate exactly one requested final image using Codex's built-in image_gen tool through the imagegen system skill. Do not write, edit, move, or delete any project file yourself; the host locates and copies the generated artifact.
Do not use the CLI fallback path, OPENAI_API_KEY, or any external API. The built-in image_gen tool is the only permitted generation path.
Do not handle patient-identifying data, private-tagged material, real-person likeness references, file attachments, filesystem image references, or data-image URLs in this profile.
Return a short factual completion note. Do not claim success unless image_gen completed."""


def media_guard(
    target: Path,
    media_kind: str,
    aspect_ratio: str | None,
    duration: int | None,
    provider: str = "grok",
) -> str:
    if provider == "codex":
        operation = (
            "Call the built-in image_gen tool exactly once. Use the complete task brief "
            "as the visual prompt"
            + (f" and choose the size closest to a {aspect_ratio} aspect ratio" if aspect_ratio else "")
            + ". Leave the generated file where the tool puts it."
        )
        return (
            f"{CODEX_MEDIA_WORKER_GUARD}\n\n{operation}\n\n"
            f"Target working directory (read-only): {target}"
        )
    if media_kind == "image":
        operation = (
            "Call image_gen exactly once. Use the complete task brief as the visual prompt"
            + (f" and set aspect_ratio to {aspect_ratio}" if aspect_ratio else "")
            + "."
        )
    else:
        operation = (
            "Create one final video. Use image_gen for a source frame and image_to_video for the final clip."
            + (f" Use aspect ratio {aspect_ratio} for the source frame." if aspect_ratio else "")
            + f" Set duration to {duration or 6} seconds."
        )
    return f"{MEDIA_WORKER_GUARD}\n\n{operation}\n\nTarget working directory (read-only): {target}"


def exit_on_termination(signum, _frame) -> None:
    """Let active context managers remove private prompts and temporary grants."""
    raise SystemExit(128 + signum)


signal.signal(signal.SIGTERM, exit_on_termination)


def profile_guard(
    profile: str,
    target: Path,
    media_kind: str | None = None,
    aspect_ratio: str | None = None,
    duration: int | None = None,
    provider: str = "grok",
) -> str:
    if profile == "media":
        return media_guard(target, media_kind or "image", aspect_ratio, duration, provider)
    guard = {
        "review": REVIEW_WORKER_GUARD,
        "research": RESEARCH_WORKER_GUARD,
        "work": WORK_WORKER_GUARD,
    }[profile]
    return f"{guard}\n\nTarget working directory: {target}"


def write_policy(provider_config: dict, profile: str) -> str:
    return provider_config.get("write_policies", {}).get(profile, "none")


def media_providers(config: dict) -> dict:
    return media_registry.providers(config)


def route_for(config: dict, host: str, provider: str, profile: str, mode: str) -> str:
    """Resolve the effective transport for this host/provider/profile/mode."""
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


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def path_has_symlink_component(path: Path) -> bool:
    absolute = path if path.is_absolute() else Path.cwd() / path
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
        if not current.exists():
            break
    return False


def harden_directory_contents(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        try:
            child.chmod(0o700 if child.is_dir() else 0o600)
        except OSError:
            pass


def secure_write_text(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    path.chmod(0o600)


def atomic_secure_json(path: Path, payload: dict) -> None:
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    secure_write_text(temp, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.replace(temp, path)
    path.chmod(0o600)


def register_launcher_sidecar() -> None:
    global LAUNCHER_LOCK_HANDLE
    raw_path = os.environ.get("CROSSCREW_LAUNCHER_SIDECAR")
    if not raw_path:
        return
    path = Path(raw_path)
    try:
        lock_path = path.with_name("launcher.lock")
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        LAUNCHER_LOCK_HANDLE = os.fdopen(lock_fd, "a+", encoding="utf-8")
        lock_path.chmod(0o600)
        fcntl.flock(LAUNCHER_LOCK_HANDLE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        atomic_secure_json(
            path,
            {
                "schema_version": "1.0",
                "pid": os.getpid(),
                "pgid": os.getpgid(0),
                "launcher_token": os.environ.get("CROSSCREW_LAUNCHER_TOKEN"),
                "lock_path": str(lock_path),
                "registered_at": utc_now(),
            },
        )
    except (OSError, BlockingIOError):
        # The supervisor also records the PID. Failure here must not prevent a
        # valid direct adapter call from returning its result envelope.
        pass


def emit(payload: dict, exit_code: int | None = None) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(payload.get("exit_code", 1) if exit_code is None else exit_code)


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        emit({"status": "config_error", "exit_code": 78, "error": str(exc)}, 78)


def resolve_state_dir(config: dict) -> Path:
    override = os.environ.get("CROSSCREW_STATE_DIR")
    if override:
        state_dir = Path(override).expanduser()
    else:
        configured = config["defaults"].get("state_dir", ".state")
        state_dir = Path(configured).expanduser()
        if not state_dir.is_absolute():
            state_dir = ROOT / state_dir
    ensure_private_dir(state_dir)
    ensure_private_dir(state_dir / "locks")
    ensure_private_dir(state_dir / "runs")
    harden_directory_contents(state_dir / "locks")
    harden_directory_contents(state_dir / "runs")
    return state_dir


def read_model_key(raw_path: str, dotted_key: str) -> str | None:
    path = Path(raw_path).expanduser()
    try:
        if path.suffix == ".toml":
            if tomllib is None:
                return None
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
        value = data
        for part in dotted_key.split("."):
            value = value[part]
        return str(value) if not isinstance(value, dict) else None
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def read_model(source: str | None) -> str | None:
    if not source or ":" not in source:
        return None
    raw_path, dotted_key = source.rsplit(":", 1)
    return read_model_key(raw_path, dotted_key)


def resolve_model(provider_config: dict, override: str | None) -> tuple[str | None, str]:
    """Return (model, provenance).

    A model this adapter cannot actually resolve is reported as None with
    provenance ``account_default_unpinned``. Naming a model we did not read from
    a real source would be a plausible-looking guess, which is worse than a
    blank: the caller could not tell a pinned model from an inferred one.
    """
    if override:
        return override, "cli_override"
    for source in provider_config.get("model_sources", []):
        kind = source.get("kind")
        if kind == "env":
            name = source.get("name", "")
            value = os.environ.get(name)
            if value:
                return value, f"env:{name}"
        elif kind == "file":
            value = read_model_key(source.get("path", ""), source.get("key", ""))
            if value:
                return value, f"file:{source.get('path')}:{source.get('key')}"
    legacy = provider_config.get("model_source")
    value = read_model(legacy)
    if value:
        return value, f"file:{legacy}"
    return None, "account_default_unpinned"


def sanitize(text: str, limit: int) -> str:
    patterns = (
        (r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+", r"\1[REDACTED]"),
        (r"(?i)\b(sk-(?:proj-)?[A-Za-z0-9_-]{16,}|gh[opusr]_[A-Za-z0-9]{20,})\b", "[REDACTED_TOKEN]"),
        (r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > limit:
        encoded = encoded[:limit] + b"\n...[truncated]"
    return encoded.decode("utf-8", errors="replace")


def sanitize_stderr(text: str, limit: int, suppress_provider_diagnostics: bool = False) -> str:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[REDACTED_EMAIL]", text)
    noisy = (
        "plugin name collision resolved by scope precedence",
        "skill name does not match expected name from path",
    )
    kept: list[str] = []
    seen: set[str] = set()
    provider_suppressed = 0
    known_suppressed = 0
    for line in text.splitlines():
        if suppress_provider_diagnostics and re.match(r"^[IWEF]\d{4}\s", line):
            provider_suppressed += 1
            continue
        if any(marker in line for marker in noisy):
            known_suppressed += 1
            continue
        normalized = line.strip()
        if normalized and normalized in seen:
            continue
        if normalized:
            seen.add(normalized)
        kept.append(line)
    if known_suppressed:
        kept.append(f"[suppressed {known_suppressed} known startup warning lines]")
    if provider_suppressed:
        kept.append(f"[suppressed {provider_suppressed} provider diagnostic lines]")
    return sanitize("\n".join(kept), limit)


def classify(returncode: int, stdout: str, stderr: str, timed_out: bool) -> tuple[str, bool]:
    combined = f"{stdout}\n{stderr}".lower()
    if timed_out:
        return "timeout", True
    if returncode == 0 and stdout.strip():
        return "ok", False
    if returncode == 0:
        return "empty_output", True
    if re.search(r"auth(?:entication)?[^\n]*(?:expired|required|failed)|login required|unauthorized|401", combined):
        return "auth_expired", False
    if (
        re.search(r"session[^\n]*(?:not found|expired|invalid)|conversation[^\n]*(?:not found|expired)", combined)
        or "no conversation found with session id" in combined
    ):
        return "session_expired", True
    if (
        "sandbox initialization failed" in combined
        or "fs_permission" in combined
        or "failed to initialize in-process app-server client: operation not permitted" in combined
        or "attempt to write a readonly database" in combined
    ):
        return "sandbox_conflict", True
    if (
        "not inside a trusted directory" in combined
        or "--skip-git-repo-check was not specified" in combined
    ):
        return "target_not_trusted", False
    if "unsafe agent" in combined or "permission denied" in combined or "operation not permitted" in combined:
        return "blocked", False
    if "already running" in combined or "still running" in combined or "resource busy" in combined:
        return "conflict", True
    return "error", True


def run_registry_path(state_dir: Path, run_id: str) -> Path:
    return run_registry.registry_path(state_dir, run_id)


def read_registry(state_dir: Path, run_id: str) -> dict:
    return run_registry.read(state_dir, run_id)


def write_registry(state_dir: Path, run_id: str, provider: str, entry: dict) -> None:
    path = run_registry_path(state_dir, run_id)
    safe_run = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)
    registry_lock = state_dir / "locks" / f"run-{safe_run}.lock"
    with registry_lock.open("a+", encoding="utf-8") as handle:
        registry_lock.chmod(0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            registry = read_registry(state_dir, run_id)
            registry.setdefault("providers", {})[provider] = entry
            registry["updated_at"] = utc_now()
            atomic_secure_json(path, registry)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def exclusive_lock(state_dir: Path, provider: str, key: str):
    safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    path = state_dir / "locks" / f"{provider}-{safe_key}.lock"
    with path.open("a+", encoding="utf-8") as handle:
        path.chmod(0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def codex_result(jsonl: str) -> tuple[str, str | None]:
    session_id = None
    messages: list[str] = []
    for line in jsonl.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            session_id = event.get("thread_id") or event.get("thread", {}).get("id")
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            value = item.get("text") or item.get("content")
            if isinstance(value, str):
                messages.append(value)
    return "\n".join(messages).strip(), session_id


def build_invocation(
    provider: str,
    command: str,
    mode: str,
    session_id: str | None,
    brief: Path,
    target: Path,
    timeout: float,
    temp_dir: Path,
    profile: str,
    model: str | None,
    media_kind: str | None = None,
    aspect_ratio: str | None = None,
    duration: int | None = None,
    effort: str | None = None,
    effort_config_key: str | None = None,
) -> tuple[list[str], str | None, Path, dict, Path | None, list[AgyProjectGrant]]:
    prompt = brief.read_text(encoding="utf-8")
    guard = profile_guard(profile, target, media_kind, aspect_ratio, duration, provider)
    guarded_prompt = f"{guard}\n\n---\nTask brief:\n\n{prompt}"
    env = os.environ.copy()
    last_message: Path | None = None
    cleanup_grants: list[AgyProjectGrant] = []

    if provider == "claude":
        argv = [
            command,
            "-p",
            "--permission-mode",
            "auto",
            "--append-system-prompt",
            guard,
            "--disable-slash-commands",
            "--output-format",
            "text",
            "--add-dir",
            str(target),
        ]
        if model:
            argv += ["--model", model]
        if mode == "oneshot":
            argv.append("--no-session-persistence")
        elif mode == "fresh":
            argv += ["--session-id", session_id]
        else:
            argv += ["--resume", session_id]
        return argv, prompt, target, env, None, cleanup_grants

    if provider == "codex":
        last_message = temp_dir / "codex-last-message.txt"
        # media generates through the built-in image_gen tool, which needs no
        # workspace write access; the host copies the artifact afterwards.
        sandbox = "workspace-write" if profile == "work" else "read-only"
        # `codex exec` refuses a target outside a git repo because an edit there
        # has no undo. Under a read-only sandbox there is nothing to undo, so the
        # guard would only block legitimate analysis of non-repo directories.
        # `work` keeps the guard.
        trust_flags = ["--skip-git-repo-check"] if profile in {"review", "research", "media"} else []
        # Only present when the caller asked for one; otherwise ~/.codex/config.toml
        # decides and the envelope reports the effort as unpinned rather than
        # naming a value we did not set.
        effort_flags = (
            ["-c", f'{effort_config_key}="{effort}"'] if effort and effort_config_key else []
        )
        if mode == "resume":
            argv = [
                command,
                "exec",
                "resume",
                "-c",
                f'sandbox_mode="{sandbox}"',
                *effort_flags,
                *trust_flags,
                "--json",
                "-o",
                str(last_message),
                session_id,
                "-",
            ]
        else:
            argv = [
                command,
                "exec",
                *effort_flags,
                "--sandbox",
                sandbox,
                *trust_flags,
                "-C",
                str(target),
                "-o",
                str(last_message),
            ]
            if mode == "oneshot":
                argv += ["--json", "--ephemeral", "-"]
            else:
                argv += ["--json", "-"]
        return argv, guarded_prompt, target, env, last_message, cleanup_grants

    if provider == "grok":
        # Worker calls must not inherit Claude-only rules, commands, or hooks,
        # including when the long-lived broker has an older environment.
        for surface in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS", "SESSIONS"):
            env[f"GROK_CLAUDE_{surface}_ENABLED"] = "false"
        env["GROK_SANDBOX"] = "workspace" if profile == "work" else "read-only"
        if profile == "media":
            for key in tuple(env):
                if (
                    re.match(r"(?i)^(?:XAI|GROK).*(?:API[_-]?KEY|TOKEN|SECRET)$", key)
                    or key.startswith("GROK_VIDEO_S3_")
                    or key in {
                        "AWS_ACCESS_KEY_ID",
                        "AWS_SECRET_ACCESS_KEY",
                        "AWS_SESSION_TOKEN",
                        "S3_ACCESS_KEY_ID",
                        "S3_SECRET_ACCESS_KEY",
                    }
                ):
                    env.pop(key, None)
        prompt_file = temp_dir / "grok-brief.md"
        secure_write_text(prompt_file, guarded_prompt)
        argv = [
            command,
            "--always-approve",
            "--permission-mode",
            "auto",
            "--no-memory",
            "--no-subagents",
            "--output-format",
            "plain",
        ]
        if profile == "media":
            tools = "image_gen" if media_kind == "image" else "image_gen,image_to_video"
            argv += ["--disable-web-search", "--tools", tools]
        if mode == "fresh":
            argv += ["--session-id", session_id]
        elif mode == "resume":
            argv += ["--resume", session_id]
        argv += ["--prompt-file", str(prompt_file)]
        return argv, None, target, env, None, cleanup_grants

    if provider == "agy":
        prompt_file = temp_dir / "agy-brief.md"
        secure_write_text(prompt_file, guarded_prompt)
        argv = [command]
        if mode == "resume":
            argv += ["--conversation", session_id]
        print_timeout = f"{max(1, int(timeout))}s" if timeout > 0 else "8760h"
        if profile == "work":
            grant = create_work_project(target)
            cleanup_grants.append(grant)
            argv += [
                "--project",
                grant.project_id,
                "--add-dir",
                str(target),
                "--add-dir",
                str(temp_dir),
                "--print",
                f"Read the complete task from {prompt_file} and complete it. Follow its profile guard.",
                "--mode",
                "accept-edits",
                "--sandbox",
                "--print-timeout",
                print_timeout,
            ]
            return argv, None, target, env, None, cleanup_grants
        argv += [
            "--add-dir",
            str(target),
            "--add-dir",
            str(temp_dir),
            "--print",
            f"Read the complete task from {prompt_file} and answer it. Follow its profile guard.",
            "--mode",
            "plan",
            "--sandbox",
            "--print-timeout",
            print_timeout,
        ]
        return argv, None, target, env, None, cleanup_grants

    raise ValueError(f"Unsupported provider: {provider}")


def run_process(argv: list[str], stdin_text: str | None, cwd: Path, env: dict, timeout: float) -> tuple[int, str, str, bool]:
    env = dict(env)
    env.pop("CROSSCREW_PROGRESS_FILE", None)
    supervised = env.get("CROSSCREW_SUPERVISED") == "1"
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
            # worker_job.py is already a process-group leader. Keeping the
            # provider in that group lets explicit cancel reach the full tree.
            # Direct adapter calls still create a group for hard-timeout cleanup.
            start_new_session=not supervised,
        )
        if timeout > 0:
            stdout, stderr = process.communicate(stdin_text, timeout=timeout)
        else:
            stdout, stderr = process.communicate(stdin_text)
        return process.returncode, stdout, stderr, False
    except subprocess.TimeoutExpired:
        try:
            process.terminate() if supervised else os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill() if supervised else os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        return 124, stdout, stderr, True
    except OSError as exc:
        return 127, "", f"launch_error: {exc}", False


def execute_once(
    args: argparse.Namespace,
    config: dict,
    provider_config: dict,
    state_dir: Path,
    brief: Path,
    mode: str,
    session_id: str | None,
) -> dict:
    command_name = provider_config["command"]
    if command_name not in ALLOWED_COMMANDS:
        return {"status": "config_error", "exit_code": 78, "error": "command is not allow-listed"}
    command = shutil.which(command_name)
    if not command:
        return {"status": "unavailable", "exit_code": 127, "error": f"command not found: {command_name}"}

    started_at = utc_now()
    began = time.monotonic()
    limit = int(config["defaults"].get("max_output_bytes", 1048576))
    stderr_limit = int(config["defaults"].get("max_stderr_bytes", 32768))
    with tempfile.TemporaryDirectory(prefix=f"multi-ai-{args.provider}-") as raw_temp:
        temp_dir = Path(raw_temp)
        argv, stdin_text, cwd, env, last_message, cleanup_grants = build_invocation(
            args.provider,
            command,
            mode,
            session_id,
            brief,
            args.target,
            args.timeout,
            temp_dir,
            args.profile,
            args.model,
            args.media_kind,
            args.aspect_ratio,
            args.duration,
            args.effort,
            effort_registry.config_key(config, args.provider),
        )
        argv, os_sandbox_applied = readonly_sandbox.wrap(
            argv, provider_config, args.profile, args.target, temp_dir
        )
        observer = progress.Writer(env) if args.provider == "codex" and env.get("CROSSCREW_PROGRESS_FILE") else None
        try:
            if observer is not None:
                returncode, raw_stdout, raw_stderr, timed_out = stream_process.run(
                    argv, stdin_text, cwd, env, args.timeout, observer, limit, stderr_limit)
            else:
                returncode, raw_stdout, raw_stderr, timed_out = run_process(argv, stdin_text, cwd, env, args.timeout)
        finally:
            if observer is not None:
                try:
                    observer.close()
                except Exception:
                    observer.degraded = True
            for grant in cleanup_grants:
                release_work_project(grant)

        parsed_session = session_id
        stdout = raw_stdout
        if args.provider == "codex" and mode != "oneshot":
            parsed_text, detected_session = codex_result(raw_stdout)
            parsed_session = detected_session or parsed_session
            if last_message and last_message.exists():
                stdout = last_message.read_text(encoding="utf-8", errors="replace")
            elif parsed_text:
                stdout = parsed_text
        elif last_message and last_message.exists():
            stdout = last_message.read_text(encoding="utf-8", errors="replace")

    stderr = sanitize_stderr(
        raw_stderr,
        stderr_limit,
        suppress_provider_diagnostics=args.provider == "agy" and returncode == 0,
    )
    stdout = sanitize(stdout, limit)
    artifacts: list[dict] = []
    artifact_error: str | None = None
    artifact_error_code: str | None = None
    if args.profile == "media" and returncode == 0 and not timed_out:
        source = provider_config.get("media_capabilities", {}).get("artifact_source")
        try:
            artifacts = collect_media_artifacts(
                artifact_source=source,
                target=args.target,
                session_id=parsed_session,
                output_dir=args.output_dir,
                media_kind=args.media_kind,
            )
        except ArtifactError as exc:
            artifact_error = str(exc)
            artifact_error_code = getattr(exc, "code", None)
    status, retryable = classify(returncode, stdout, stderr, timed_out)
    if args.profile == "media" and returncode == 0 and not timed_out:
        # Precedence: a verified artifact, then the provider's own explicit
        # reason, then our inferences about missing directories. The provider
        # naming the cause beats us guessing from a absent folder, which is
        # usually just a consequence of that cause.
        if artifacts:
            status, retryable = "ok", False
        elif (
            args.media_kind == "video"
            and "zero data retention teams must provide output.upload_url"
            in f"{stdout}\n{stderr}".lower()
        ):
            status, retryable = "zdr_output_required", False
        elif artifact_error_code == "artifact_root_missing":
            # Our assumption about where this provider stores output no longer
            # holds. Retrying cannot fix that; the layout has to be re-checked.
            status, retryable = "artifact_source_moved", False
        elif artifact_error_code == "artifact_session_missing":
            # The provider ran but left no session directory: generation did not
            # happen. That is worth another attempt.
            status, retryable = "artifact_missing", True
        elif artifact_error:
            status, retryable = "artifact_invalid", False
        else:
            status, retryable = "artifact_missing", True
    model, model_source = resolve_model(provider_config, args.model)
    result = {
        "schema_version": "2.0",
        "status": status,
        "exit_code": returncode,
        "retryable": retryable,
        "provider": args.provider,
        "host": args.host,
        "transport": "direct",
        "model": model,
        "model_source": model_source,
        "model_pinned": model is not None,
        "reasoning_effort": args.effort,
        "reasoning_effort_source": "explicit" if args.effort else "provider_default_unpinned",
        "run_id": args.run_id,
        "session_id": parsed_session,
        "mode": mode,
        "profile": args.profile,
        "target": str(args.target),
        "started_at": started_at,
        "duration_seconds": round(time.monotonic() - began, 3),
        "stdout": stdout,
        "stderr_sanitized": stderr,
        "write_policy": write_policy(provider_config, args.profile),
        "os_sandbox": os_sandbox_applied,
        "rehydrated": False,
    }
    if observer is not None:
        result["progress_degraded"] = observer.degraded or observer.decoder.dropped
    if readonly_sandbox.enabled_for(provider_config, args.profile) and not os_sandbox_applied:
        # The registry asked for OS confinement and we could not apply it. Say so
        # rather than letting the write_policy name imply a guarantee.
        result["os_sandbox_warning"] = "requested but unavailable on this host"
    elif os_sandbox_applied and readonly_sandbox.target_inside_writable_scope(
        provider_config, args.profile, args.target, Path(tempfile.gettempdir())
    ):
        result["os_sandbox_warning"] = (
            "target sits inside the provider's own writable state or temp tree, "
            "so read-only confinement does not cover it"
        )
    if args.profile == "media":
        result.update(
            {
                "capability": "media",
                "media_kind": args.media_kind,
                "media_model": "provider-managed-grok-imagine",
                "output_dir": str(args.output_dir),
                "artifacts": artifacts,
            }
        )
        if artifact_error:
            result["artifact_error"] = artifact_error
            result["artifact_error_code"] = artifact_error_code
        if status == "artifact_source_moved":
            result["hint"] = (
                "The provider's artifact storage root is not where the registry "
                "expects it. Re-check media_capabilities.artifact_source and the "
                "path constants in media_artifacts.py against the provider's "
                "current layout; this is not a generation failure."
            )
        if status == "zdr_output_required":
            privacy = video_privacy_preflight()
            result["privacy_scope"] = privacy["privacy_scope"]
            result["remediation"] = (
                "Keep the current privacy boundary and configure a private, mode-0600 "
                "S3-compatible target under tools.zdr_video_output_s3. Personal "
                "/privacy opt-out and team ZDR both require output.upload_url for video. "
                "Do not retry until private output is configured."
            )
    if status == "sandbox_conflict":
        result["requires_host_escalation"] = True
    if status == "target_not_trusted":
        result["hint"] = (
            "The provider refused this target because it is outside a version-controlled "
            "directory and the profile allows writes. Point --target at a git repository, "
            "or use --profile review/research when read-only analysis is enough."
        )
    return result


def broker_payload(args: argparse.Namespace, action: str) -> dict:
    """Build a typed broker request that mirrors this invocation.

    The broker validates every field again and constructs its own argv, so this
    is a description of intent rather than a command.
    """
    payload: dict = {
        "action": action,
        "provider": args.provider,
        "host": args.host,
        "profile": args.profile,
        "mode": args.mode,
        "brief": str(args.brief_resolved),
        "target": str(args.target),
    }
    if args.run_id:
        payload["run_id"] = args.run_id
    if args.session_id:
        payload["session_id"] = args.session_id
    if args.model:
        payload["model"] = args.model
    if args.effort:
        payload["effort"] = args.effort
    if args.media_kind:
        payload["media_kind"] = args.media_kind
    if args.output_dir:
        payload["output_dir"] = str(args.output_dir)
    if args.aspect_ratio:
        payload["aspect_ratio"] = args.aspect_ratio
    if args.duration:
        payload["duration"] = args.duration
    if args.rehydrate_file:
        payload["rehydrate_file"] = str(args.rehydrate_file.expanduser().resolve())
    return payload


def make_rehydration_brief(current: Path, transcript: Path, temp_dir: Path) -> Path:
    output = temp_dir / "rehydrated-brief.md"
    output.write_text(
        current.read_text(encoding="utf-8")
        + "\n\n---\nPrevious session transcript for context only:\n\n"
        + transcript.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return output


def check_provider(args: argparse.Namespace, provider_config: dict, config: dict) -> None:
    command_name = provider_config["command"]
    allowed = command_name in ALLOWED_COMMANDS
    command = shutil.which(command_name) if allowed else None
    route = route_for(config, args.host, args.provider, args.profile, args.mode)
    model, model_source = resolve_model(provider_config, args.model)
    broker_health = None
    broker_unavailable_reason = None
    if route == "broker":
        state = resolve_state_dir(config)
        broker_health = broker_client.health(state)
        if broker_health is None:
            broker_unavailable_reason = broker_client.unavailable_reason(state)
    payload = {
        "schema_version": "2.0",
        "status": "ready" if command else ("config_error" if not allowed else "unavailable"),
        "exit_code": 0 if command else (78 if not allowed else 127),
        "provider": args.provider,
        "host": args.host,
        "command": command,
        "model": model,
        "model_source": model_source,
        "model_pinned": model is not None,
        "modes": provider_config.get("modes", []),
        "profiles": provider_config.get("profiles", []),
        "profile": args.profile,
        "write_policy": write_policy(provider_config, args.profile),
        "route": route,
        "requires_host_escalation": route in {"broker", "requires_host_escalation"},
    }
    if route == "broker":
        payload["broker_available"] = broker_health is not None
        payload["broker_unavailable_reason"] = broker_unavailable_reason
        payload["escalation_ready"] = broker_health is not None
        if broker_health is None:
            payload["hint"] = (
                "This host cannot spawn the provider itself and the escalation "
                "broker is not reachable. Run broker_service.sh install, or "
                "re-run the command outside the host sandbox with --host-escalated."
            )
    elif route == "in_process":
        payload["hint"] = (
            "Self-delegation. The host answers in-process; pass "
            "--allow-self-delegation only for a deliberate second opinion."
        )
    if args.profile == "media":
        payload["media_kinds"] = media_registry.kinds(config, args.provider)
        payload["media_providers"] = media_registry.providers(config)
        if args.provider == "grok":
            privacy = video_privacy_preflight()
            payload["video_ready"] = privacy["ready"]
            payload["privacy_scope"] = privacy["privacy_scope"]
            if not privacy["ready"]:
                payload["video_hint"] = (
                    "Image generation is ready. Video needs a private S3-compatible "
                    "output target: grok_video_output_setup.py install."
                )
    emit(payload, payload["exit_code"])


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("provider", choices=sorted(ALLOWED_COMMANDS))
    value.add_argument("brief", nargs="?", type=Path)
    value.add_argument("--host", choices=ALLOWED_HOSTS, default="shell")
    value.add_argument("--target", type=Path, default=Path.cwd())
    value.add_argument("--mode", choices=("oneshot", "fresh", "resume"), default="oneshot")
    value.add_argument("--profile", choices=("review", "work", "research", "media"), default="review")
    value.add_argument("--model")
    # Allowed values come from the registry, not from argparse: the provider that
    # accepts an effort and the values it accepts are registry facts, and a stale
    # choices= list here would drift from them silently.
    value.add_argument("--effort")
    value.add_argument("--media-kind", choices=("image", "video"))
    value.add_argument("--output-dir", type=Path)
    value.add_argument(
        "--aspect-ratio",
        choices=("auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "2:1", "1:2", "19.5:9", "9:19.5", "20:9", "9:20"),
    )
    value.add_argument("--duration", type=int, choices=(6, 10))
    value.add_argument("--session-id")
    value.add_argument("--run-id", default=f"run-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}")
    value.add_argument("--timeout", type=float)
    value.add_argument("--rehydrate-file", type=Path)
    value.add_argument("--host-escalated", action="store_true")
    value.add_argument(
        "--allow-self-delegation",
        action="store_true",
        help="permit a host to spawn its own provider (default: answer in-process)",
    )
    value.add_argument("--check", action="store_true")
    return value


def main() -> None:
    register_launcher_sidecar()
    args = parser().parse_args()
    config = load_config()
    provider_config = config.get("providers", {}).get(args.provider)
    if not provider_config:
        emit({"status": "config_error", "exit_code": 78, "error": "provider missing from config"}, 78)

    if args.timeout is None:
        args.timeout = float(config["defaults"].get("timeout_seconds", 0))
    args.target = args.target.expanduser().resolve()
    if not args.check:
        blocked = billing.refusal(args.provider, args.target)
        if blocked:
            emit(blocked, 78)
    if args.profile not in provider_config.get("profiles", []):
        payload = {
            "status": "unsupported_profile",
            "exit_code": 64,
            "provider": args.provider,
            "profile": args.profile,
            "supported_profiles": provider_config.get("profiles", []),
        }
        if args.profile == "media":
            payload["media_providers"] = media_providers(config)
            payload["hint"] = (
                f"{args.provider} has no media capability. "
                f"Route generation to {media_providers(config)}."
            )
        emit(payload, 64)
    if args.model and args.provider != "claude":
        emit(
            {
                "status": "unsupported_model_override",
                "exit_code": 64,
                "provider": args.provider,
                "model": args.model,
                "hint": "Per-job model override is currently supported only for Claude.",
            },
            64,
        )
    effort_error = effort_registry.validation_error(config, args.provider, args.effort)
    if effort_error:
        status, hint = effort_error
        emit(
            {
                "status": status,
                "exit_code": 64,
                "provider": args.provider,
                "effort": args.effort,
                "supported_efforts": effort_registry.values(config, args.provider),
                "effort_providers": effort_registry.providers(config),
                "hint": hint,
            },
            64,
        )
    media_options_present = any(
        value is not None
        for value in (args.media_kind, args.output_dir, args.aspect_ratio, args.duration)
    )
    if args.profile == "media":
        media_error = media_registry.validation_error(
            config, args.provider, args.media_kind, args.aspect_ratio, args.duration
        )
        if media_error:
            status, hint = media_error
            emit(
                {
                    "status": status,
                    "exit_code": 64,
                    "provider": args.provider,
                    "profile": args.profile,
                    "media_kind": args.media_kind,
                    "supported_kinds": media_registry.kinds(config, args.provider),
                    "media_providers": media_registry.providers(config),
                    "hint": hint,
                },
                64,
            )
        # --check is a capability probe, not a generation request: it must be able
        # to answer "can this host reach this provider for media?" without being
        # handed a media kind, an output directory, or a fresh session.
        if args.mode != "fresh" and not args.check:
            emit(
                {
                    "status": "invalid_media_mode",
                    "exit_code": 64,
                    "mode": args.mode,
                    "hint": "Media generation requires --mode fresh.",
                },
                64,
            )
        if (not args.media_kind or not args.output_dir) and not args.check:
            emit(
                {
                    "status": "invalid_media_input",
                    "exit_code": 64,
                    "error": "--media-kind and --output-dir are required for profile media",
                },
                64,
            )
        if args.check:
            check_provider(args, provider_config, config)
        raw_output = args.output_dir.expanduser()
        if path_has_symlink_component(raw_output):
            emit(
                {
                    "status": "invalid_media_output",
                    "exit_code": 64,
                    "error": "output directory path must not contain symlinks",
                },
                64,
            )
        try:
            raw_output.mkdir(parents=True, exist_ok=True, mode=0o700)
            raw_output.chmod(0o700)
        except OSError as exc:
            emit({"status": "invalid_media_output", "exit_code": 73, "error": str(exc)}, 73)
        args.output_dir = raw_output.resolve()
        if not args.output_dir.is_dir():
            emit({"status": "invalid_media_output", "exit_code": 66, "error": "output path is not a directory"}, 66)
    elif media_options_present:
        emit(
            {
                "status": "invalid_media_input",
                "exit_code": 64,
                "error": "media options require --profile media",
            },
            64,
        )
    if args.check:
        check_provider(args, provider_config, config)

    if args.mode not in provider_config.get("modes", []):
        emit({"status": "unsupported_mode", "exit_code": 64, "provider": args.provider, "mode": args.mode}, 64)
    if not args.brief:
        emit({"status": "invalid_input", "exit_code": 64, "error": "brief path is required"}, 64)
    if ".." in args.brief.parts:
        emit({"status": "invalid_input", "exit_code": 64, "error": "brief path must not contain '..'"}, 64)
    brief = args.brief.expanduser().resolve()
    if not brief.is_file():
        emit({"status": "invalid_input", "exit_code": 66, "error": f"brief not found: {brief}"}, 66)
    args.brief_resolved = brief
    if args.profile == "media":
        media_error = validate_media_brief(brief.read_text(encoding="utf-8", errors="replace"))
        if media_error:
            emit(
                {
                    "status": media_error,
                    "exit_code": 65,
                    "provider": args.provider,
                    "profile": args.profile,
                    "hint": "Use text-only prompts in the initial media profile. Reference media and patient-identifying data are blocked.",
                },
                65,
            )
        if args.media_kind == "video":
            privacy = video_privacy_preflight()
            if not privacy["ready"]:
                hint = (
                    "The S3 config is complete but its file permissions expose secrets; "
                    "set the config file to mode 0600."
                    if privacy["private_output_complete"]
                    and not privacy["private_output_permissions_ok"]
                    else "Configure a private S3-compatible output target in Grok Build."
                )
                emit(
                    {
                        "schema_version": "2.0",
                        "status": "zdr_output_required",
                        "exit_code": 78,
                        "retryable": False,
                        "provider": args.provider,
                        "profile": args.profile,
                        "media_kind": args.media_kind,
                        "privacy_scope": privacy["privacy_scope"],
                        "private_output_configured": privacy["private_output_configured"],
                        "config_source": privacy["config_source"],
                        "hint": hint,
                        "remediation": (
                            "Keep Privacy mode and configure tools.zdr_video_output_s3 "
                            "with an HTTPS endpoint and a mode-0600 config file. "
                            "No Grok generation request was started."
                        ),
                    },
                    78,
                )
    if not args.target.is_dir():
        emit({"status": "invalid_input", "exit_code": 66, "error": f"target directory not found: {args.target}"}, 66)

    state_dir = resolve_state_dir(config)
    # 소유권을 여기서 확정한다. 이후의 lock·spawn·registry write가 전부 이 판정
    # 위에서 돈다. read-time 확인만으로는 그 사이에 남이 파일을 만들 수 있다.
    try:
        run_registry.claim(state_dir, args.run_id)
    except run_registry.RunIdCollision as exc:
        emit(
            {
                "status": "run_id_collision",
                "exit_code": 64,
                "run_id": args.run_id,
                "conflicting_run_id": exc.stored,
                "registry_file": exc.path.name,
                # 두 원인은 조치가 다르다. 하나로 뭉뚱그리면 손상된 경우에
                # 사용자가 빠져나올 방법을 모른다.
                "hint": (
                    f"run ID '{exc.stored}'가 이미 이 파일을 소유한다. "
                    "run ID를 [A-Za-z0-9_.-] 안에서 다르게 짓는다."
                    if exc.stored is not None else
                    f"registry 파일이 손상됐다: {exc.path}. 조용히 덮어쓰면 살아 있는 "
                    "session ID가 사라지므로 막는다. 내용을 확인하고, 버려도 되면 "
                    "그 파일을 지운 뒤 다시 실행한다."
                ),
            },
            64,
        )
    route = route_for(config, args.host, args.provider, args.profile, args.mode)

    if route == "in_process" and not args.allow_self_delegation:
        emit(
            {
                "schema_version": "2.0",
                "status": "self_delegation",
                "exit_code": 64,
                "retryable": False,
                "provider": args.provider,
                "host": args.host,
                "route": route,
                "hint": (
                    "The host and the provider are the same agent. Answer in-process "
                    "instead of spawning a second copy, or pass --allow-self-delegation "
                    "for a deliberate independent second opinion."
                ),
            },
            64,
        )

    if route in {"broker", "requires_host_escalation"} and not args.host_escalated:
        brokered = None
        broker_error = None
        if route == "broker":
            try:
                brokered = broker_client.request(state_dir, broker_payload(args, "call"))
            except broker_client.BrokerUnavailable as exc:
                broker_error = str(exc)
        if brokered is not None:
            emit(brokered, 0 if brokered.get("status") == "ok" else brokered.get("exit_code") or 1)
        emit(
            {
                "schema_version": "2.0",
                "status": "needs_host_escalation",
                "exit_code": 77,
                "retryable": True,
                "provider": args.provider,
                "host": args.host,
                "route": route,
                "requires_host_escalation": True,
                "broker_available": False,
                "broker_error": broker_error,
                "hint": (
                    "This host cannot spawn the provider itself. Start the escalation "
                    "broker with broker_service.sh install, or re-run the same adapter "
                    "command outside the host sandbox and add --host-escalated."
                ),
            },
            77,
        )

    registry = read_registry(state_dir, args.run_id)
    session_id = args.session_id
    if args.mode == "resume" and not session_id:
        registry_entry = registry.get("providers", {}).get(args.provider, {})
        session_id = registry_entry.get("session_id")
        if not session_id:
            emit(
                {
                    "status": "session_missing",
                    "exit_code": 65,
                    "provider": args.provider,
                    "run_id": args.run_id,
                    "hint": "Provide --session-id or reuse a run ID that has a successful fresh call.",
                },
                65,
            )
        previous_profile = registry_entry.get("profile", "review")
        if previous_profile != args.profile:
            emit(
                {
                    "status": "profile_mismatch",
                    "exit_code": 65,
                    "provider": args.provider,
                    "run_id": args.run_id,
                    "profile": args.profile,
                    "previous_profile": previous_profile,
                    "hint": "Start a fresh session, or pass an explicit verified --session-id to change profile.",
                },
                65,
            )
        updated_at = registry_entry.get("updated_at")
        max_age = float(config["defaults"].get("resume_max_age_seconds", 604800))
        try:
            updated = dt.datetime.fromisoformat(updated_at) if updated_at else None
            age = (dt.datetime.now(dt.timezone.utc) - updated).total_seconds() if updated else max_age + 1
        except (TypeError, ValueError):
            age = max_age + 1
        if max_age > 0 and age > max_age:
            emit(
                {
                    "status": "session_stale",
                    "exit_code": 65,
                    "provider": args.provider,
                    "run_id": args.run_id,
                    "session_id": session_id,
                    "age_seconds": round(age, 3),
                    "hint": "Start a fresh session or pass --session-id explicitly after verifying it.",
                },
                65,
            )
    elif args.mode == "fresh" and args.provider in {"claude", "grok"} and not session_id:
        session_id = str(uuid.uuid4())

    lock_key = session_id or args.run_id
    with exclusive_lock(state_dir, args.provider, lock_key) as acquired:
        if not acquired:
            emit(
                {
                    "status": "conflict",
                    "exit_code": 75,
                    "retryable": True,
                    "provider": args.provider,
                    "session_id": session_id,
                    "hint": "This provider session is already active; do not reuse it concurrently.",
                },
                75,
            )

        result = execute_once(args, config, provider_config, state_dir, brief, args.mode, session_id)

        # A route marked `direct` can still meet a sandbox it did not expect --
        # a host may tighten its policy, or a provider may nest a new sandbox of
        # its own. Rather than hand that back as a dead end, escalate once
        # through the broker when one is running.
        if (
            result["status"] == "sandbox_conflict"
            and not args.host_escalated
            and not broker_client.in_broker_child()
        ):
            try:
                brokered = broker_client.request(state_dir, broker_payload(args, "call"))
            except broker_client.BrokerUnavailable:
                brokered = None
            if brokered is not None and brokered.get("status") not in {
                "broker_request_rejected",
                "broker_bad_envelope",
                "broker_spawn_failed",
            }:
                brokered["auto_escalated"] = True
                brokered["direct_attempt_status"] = "sandbox_conflict"
                emit(brokered, 0 if brokered.get("status") == "ok" else brokered.get("exit_code") or 1)

        if result["status"] == "session_expired" and args.rehydrate_file:
            transcript = args.rehydrate_file.expanduser().resolve()
            if transcript.is_file():
                with tempfile.TemporaryDirectory(prefix="multi-ai-rehydrate-") as raw_temp:
                    rehydrated_brief = make_rehydration_brief(brief, transcript, Path(raw_temp))
                    replacement_session = str(uuid.uuid4()) if args.provider in {"claude", "grok"} else None
                    replacement_mode = "fresh" if args.provider in {"claude", "codex", "grok"} else "oneshot"
                    result = execute_once(
                        args, config, provider_config, state_dir, rehydrated_brief, replacement_mode, replacement_session
                    )
                    result["rehydrated"] = True
                    result["replaced_session_id"] = session_id

        write_registry(
            state_dir,
            args.run_id,
            args.provider,
            {
                "session_id": result.get("session_id"),
                "status": result["status"],
                "mode": result["mode"],
                "profile": result["profile"],
                "updated_at": utc_now(),
            },
        )
        emit(result, 0 if result["status"] == "ok" else result["exit_code"] or 1)


if __name__ == "__main__":
    main()
