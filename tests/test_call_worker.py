import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "call_worker.py"


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.state = self.base / "state"
        self.brief = self.base / "brief.md"
        self.brief.write_text("Give an independent view.", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def fake(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(0o755)

    def invoke(self, provider, *extra, timeout=10):
        env = self.environment()
        completed = subprocess.run(
            ["python3", str(SCRIPT), provider, str(self.brief), "--target", str(self.base), *extra],
            text=True,
            capture_output=True,
            env=env,
            timeout=timeout,
        )
        return completed, json.loads(completed.stdout)

    def environment(self):
        env = os.environ.copy()
        for name in ("CROSSCREW_SUPERVISED", "CROSSCREW_LAUNCHER_SIDECAR", "CROSSCREW_LAUNCHER_TOKEN"):
            env.pop(name, None)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["CROSSCREW_STATE_DIR"] = str(self.state)
        env["CROSSCREW_AGY_PROJECTS_DIR"] = str(self.base / "agy-projects")
        env["HOME"] = str(self.base)
        env["GROK_HOME"] = str(self.base / ".grok")
        return env

    def test_success_has_standard_envelope(self):
        self.fake("claude", "printf 'FAKE_OK\\n'")
        completed, payload = self.invoke("claude", "--host", "codex", "--host-escalated")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["provider"], "claude")
        self.assertIn("FAKE_OK", payload["stdout"])
        self.assertNotIn("--permission-mode plan", payload["stdout"])

    def test_claude_uses_auto_mode_with_no_edit_system_guard(self):
        self.fake("claude", "printf '%s\\n' \"$*\"")
        completed, payload = self.invoke("claude")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--permission-mode auto", payload["stdout"])
        self.assertIn("--append-system-prompt", payload["stdout"])
        self.assertIn("--output-format json", payload["stdout"])
        self.assertIn("run diagnostics, tests, builds", payload["stdout"])
        self.assertIn("Do not intentionally edit", payload["stdout"])
        self.assertNotIn("--tools", payload["stdout"])
        self.assertNotIn("--permission-mode plan", payload["stdout"])
        self.assertEqual(payload["profile"], "review")
        self.assertEqual(payload["write_policy"], "prompt_guarded_read_only")

    def test_claude_work_profile_uses_broad_target_scoped_guard(self):
        self.fake("claude", "printf '%s\\n' \"$*\"")
        completed, payload = self.invoke("claude", "--profile", "work")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--permission-mode auto", payload["stdout"])
        self.assertIn("Complete the requested task end-to-end", payload["stdout"])
        self.assertIn("create, edit, move, and delete files inside the target", payload["stdout"])
        self.assertIn(str(self.base), payload["stdout"])
        self.assertEqual(payload["profile"], "work")
        self.assertEqual(payload["write_policy"], "prompt_guarded_target_write")

    def test_claude_model_override_is_forwarded_and_reported(self):
        self.fake("claude", "printf '%s\n' \"$*\"")
        completed, payload = self.invoke("claude", "--model", "opus")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--model opus", payload["stdout"])
        self.assertEqual(payload["model"], "opus")

    def test_model_override_rejects_unsupported_provider(self):
        self.fake("grok", "printf 'SHOULD_NOT_RUN\n'")
        completed, payload = self.invoke("grok", "--model", "opus")
        self.assertEqual(completed.returncode, 64)
        self.assertEqual(payload["status"], "unsupported_model_override")

    def test_codex_work_profile_uses_workspace_write_and_guarded_prompt(self):
        self.fake(
            "codex",
            "args=\"$*\"; out=''; while [ $# -gt 0 ]; do if [ \"$1\" = '-o' ]; then shift; out=$1; fi; shift; done; "
            "body=$(cat); printf '%s\\n%s\\n' \"$args\" \"$body\" > \"$out\"",
        )
        completed, payload = self.invoke("codex", "--profile", "work")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--sandbox workspace-write", payload["stdout"])
        self.assertIn("--json --ephemeral", payload["stdout"])
        self.assertIn("Complete the requested task end-to-end", payload["stdout"])
        self.assertIn("Give an independent view.", payload["stdout"])
        self.assertEqual(payload["write_policy"], "sandboxed_target_write")

    def test_grok_work_profile_uses_custom_sandbox_and_private_guarded_prompt(self):
        import grok_sandbox
        config = self.base / ".grok" / "sandbox.toml"
        config.parent.mkdir()
        config.write_text(grok_sandbox.stanza())
        self.fake(
            "grok",
            "prompt=''; while [ $# -gt 0 ]; do if [ \"$1\" = '--prompt-file' ]; then shift; prompt=$1; fi; shift; done; "
            "printf 'SANDBOX=%s\\n' \"$GROK_SANDBOX\"; cat \"$prompt\"",
        )
        completed, payload = self.invoke("grok", "--profile", "work")
        self.assertEqual(completed.returncode, 0)
        self.assertIn(f"SANDBOX={grok_sandbox.NAME}", payload["stdout"])
        self.assertIn("Complete the requested task end-to-end", payload["stdout"])
        self.assertIn("Give an independent view.", payload["stdout"])
        self.assertEqual(payload["write_policy"], "sandboxed_target_write")

    def test_agy_work_profile_uses_temporary_target_grant_without_dangerous_skip(self):
        self.fake(
            "agy",
            "args=\"$*\"; project=''; while [ $# -gt 0 ]; do if [ \"$1\" = '--project' ]; then shift; project=$1; fi; shift; done; "
            "printf '%s\\n' \"$args\"; cat \"$CROSSCREW_AGY_PROJECTS_DIR/$project.json\"",
        )
        completed, payload = self.invoke("agy", "--profile", "work")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--mode accept-edits", payload["stdout"])
        self.assertIn("--sandbox", payload["stdout"])
        self.assertNotIn("dangerously-skip-permissions", payload["stdout"])
        self.assertIn(f"write_file({self.base.resolve()})", payload["stdout"])
        self.assertEqual(payload["write_policy"], "os_sandbox_project_grant_target_write")
        self.assertEqual(list((self.base / "agy-projects").glob("crosscrew-work-*.json")), [])
        self.assertEqual(list((self.base / "agy-projects").glob("crosscrew-work-*.lock")), [])

    def test_timeout_kills_process_group(self):
        self.fake("grok", "sleep 2")
        completed, payload = self.invoke("grok", "--timeout", "0.1")
        self.assertEqual(completed.returncode, 124)
        self.assertEqual(payload["status"], "timeout")
        self.assertTrue(payload["retryable"])

    def test_supervised_timeout_returns_json_without_signaling_supervisor_group(self):
        self.fake("grok", "sleep 2")
        env = self.environment()
        env["CROSSCREW_SUPERVISED"] = "1"
        completed = subprocess.run(
            [
                "python3", str(SCRIPT), "grok", str(self.brief), "--target", str(self.base),
                "--timeout", "0.1",
            ],
            text=True,
            capture_output=True,
            env=env,
            timeout=5,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 124)
        self.assertEqual(payload["status"], "timeout")

    def test_codex_host_claude_persistent_modes_require_escalation(self):
        self.fake("claude", "printf 'SHOULD_NOT_RUN\\n'")
        for mode in ("fresh", "resume"):
            extra = ["--mode", mode, "--host", "codex"]
            if mode == "resume":
                extra += ["--session-id", "known-session"]
            completed, payload = self.invoke("claude", *extra)
            self.assertEqual(completed.returncode, 77)
            self.assertEqual(payload["status"], "needs_host_escalation")

    def test_codex_host_claude_oneshot_requires_escalation(self):
        self.fake("claude", "printf 'SHOULD_NOT_RUN\\n'")
        completed, payload = self.invoke("claude", "--host", "codex")
        self.assertEqual(completed.returncode, 77)
        self.assertEqual(payload["status"], "needs_host_escalation")

    def test_media_profile_accepts_empty_stdout_when_verified_image_exists(self):
        encoded_target = quote(str(self.base.resolve()), safe="")
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nksAAAAASUVORK5CYII="
        self.fake(
            "grok",
            "sid=''; while [ $# -gt 0 ]; do "
            "if [ \"$1\" = '--session-id' ]; then shift; sid=$1; fi; shift; done; "
            f"dest=\"$HOME/.grok/sessions/{encoded_target}/$sid/images\"; "
            "mkdir -p \"$dest\"; "
            f"printf '%s' '{png}' | /usr/bin/base64 -D > \"$dest/1.png\"",
        )
        output_dir = self.base.resolve() / "media-output"
        completed, payload = self.invoke(
            "grok",
            "--profile",
            "media",
            "--mode",
            "fresh",
            "--media-kind",
            "image",
            "--output-dir",
            str(output_dir),
        )
        self.assertEqual(completed.returncode, 0, payload)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["capability"], "media")
        self.assertEqual(len(payload["artifacts"]), 1)
        artifact = payload["artifacts"][0]
        self.assertEqual(artifact["mime_type"], "image/png")
        self.assertEqual(artifact["width"], 1)
        self.assertEqual(artifact["height"], 1)
        self.assertTrue(Path(artifact["path"]).is_file())
        self.assertEqual(Path(artifact["path"]).parent, output_dir.resolve())

    def test_media_profile_requires_fresh_mode_and_explicit_output(self):
        self.fake("grok", "printf 'SHOULD_NOT_RUN\\n'")
        completed, payload = self.invoke("grok", "--profile", "media")
        self.assertEqual(completed.returncode, 64)
        self.assertEqual(payload["status"], "invalid_media_mode")

    def test_media_output_rejects_symlink_in_any_path_component(self):
        self.fake("grok", "printf 'SHOULD_NOT_RUN\\n'")
        real = self.base / "real-output"
        real.mkdir()
        link = self.base / "linked-output"
        link.symlink_to(real, target_is_directory=True)
        completed, payload = self.invoke(
            "grok",
            "--profile",
            "media",
            "--mode",
            "fresh",
            "--media-kind",
            "image",
            "--output-dir",
            str(link / "nested"),
        )
        self.assertEqual(completed.returncode, 64)
        self.assertEqual(payload["status"], "invalid_media_output")
        self.assertFalse((real / "nested").exists())

    def video_media_call(self):
        return self.invoke(
            "grok",
            "--profile",
            "media",
            "--mode",
            "fresh",
            "--media-kind",
            "video",
            "--duration",
            "6",
            "--output-dir",
            str(self.base.resolve() / "media-output"),
        )

    def write_grok_auth(self, **fields):
        home = self.base / ".grok"
        home.mkdir(mode=0o700, exist_ok=True)
        (home / "auth.json").write_text(json.dumps({"scope": fields}), encoding="utf-8")

    def test_unverifiable_privacy_state_blocks_video_before_spending_anything(self):
        # No auth record: we cannot tell whether a private destination is needed,
        # so the preflight must refuse rather than let the request through and
        # burn a paid source frame on a call the API rejects.
        self.fake("grok", "printf 'SHOULD_NOT_RUN\\n'")
        completed, payload = self.video_media_call()
        self.assertEqual(completed.returncode, 78)
        self.assertEqual(payload["status"], "zdr_output_required")
        self.assertFalse(payload["retryable"])
        self.assertIn("tools.zdr_video_output_s3", payload["remediation"])
        self.assertIn("No Grok generation request was started", payload["remediation"])
        self.assertNotIn("SHOULD_NOT_RUN", json.dumps(payload))

    def test_renamed_privacy_fields_also_block_video(self):
        self.fake("grok", "printf 'SHOULD_NOT_RUN\\n'")
        self.write_grok_auth(auth_mode="oidc", data_retention_preference="opt_out")
        completed, payload = self.video_media_call()
        self.assertEqual(completed.returncode, 78)
        self.assertEqual(payload["status"], "zdr_output_required")
        self.assertEqual(payload["privacy_scope"], "schema_unrecognized")

    def test_video_zdr_upload_requirement_is_terminal_and_actionable(self):
        # Recognized unrestricted account: the preflight lets it through, so the
        # post-hoc detection of the provider's own ZDR error still has to work.
        self.write_grok_auth(team_blocked_reasons=[], coding_data_retention_opt_out=False)
        self.fake(
            "grok",
            "printf 'Video generation failed: Zero Data Retention teams must provide output.upload_url\\n'",
        )
        completed, payload = self.video_media_call()
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(payload["status"], "zdr_output_required")
        self.assertFalse(payload["retryable"])
        self.assertIn("tools.zdr_video_output_s3", payload["remediation"])

    def test_media_profile_blocks_reference_paths_and_phi(self):
        self.fake("grok", "printf 'SHOULD_NOT_RUN\\n'")
        output_dir = self.base.resolve() / "media-output"
        for text, expected in (
            ("Use /tmp/private-face.jpg as a reference.", "reference_media_blocked"),
            ("환자번호: P-12345의 얼굴", "phi_blocked"),
        ):
            self.brief.write_text(text, encoding="utf-8")
            completed, payload = self.invoke(
                "grok",
                "--profile",
                "media",
                "--mode",
                "fresh",
                "--media-kind",
                "image",
                "--output-dir",
                str(output_dir),
            )
            self.assertEqual(completed.returncode, 65)
            self.assertEqual(payload["status"], expected)

    def test_media_profile_blocks_known_api_override_before_generation(self):
        encoded_target = quote(str(self.base.resolve()), safe="")
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nksAAAAASUVORK5CYII="
        self.fake(
            "grok",
            "sid=''; while [ $# -gt 0 ]; do "
            "if [ \"$1\" = '--session-id' ]; then shift; sid=$1; fi; shift; done; "
            f"dest=\"$HOME/.grok/sessions/{encoded_target}/$sid/images\"; "
            "mkdir -p \"$dest\"; "
            f"printf '%s' '{png}' | /usr/bin/base64 -D > \"$dest/1.png\"; "
            "printf 'XAI=%s GROK=%s VIDEO_S3=%s AWS=%s\\n' "
            "\"${XAI_API_KEY-unset}\" \"${GROK_API_TOKEN-unset}\" "
            "\"${GROK_VIDEO_S3_SECRET_ACCESS_KEY-unset}\" \"${AWS_SECRET_ACCESS_KEY-unset}\"",
        )
        env = self.environment()
        env["XAI_API_KEY"] = "secret-xai"
        env["GROK_API_TOKEN"] = "secret-grok"
        env["GROK_VIDEO_S3_SECRET_ACCESS_KEY"] = "secret-video"
        env["AWS_SECRET_ACCESS_KEY"] = "secret-aws"
        completed = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "grok",
                str(self.brief),
                "--target",
                str(self.base),
                "--profile",
                "media",
                "--mode",
                "fresh",
                "--media-kind",
                "image",
                "--output-dir",
                str(self.base.resolve() / "media-output"),
            ],
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 78, payload)
        self.assertEqual(payload["status"], "billing_configuration_blocked")
        self.assertFalse((self.base / "media-output").exists())
        self.assertNotIn("secret-xai", completed.stdout)

    def test_claude_no_conversation_message_is_session_expired(self):
        self.fake("claude", "printf 'No conversation found with session ID: missing\\n' >&2; exit 1")
        completed, payload = self.invoke(
            "claude", "--mode", "resume", "--session-id", "missing", "--host-escalated"
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(payload["status"], "session_expired")

    def test_auth_failure_is_not_blindly_retried(self):
        self.fake("claude", "printf 'Authentication expired\\n' >&2; exit 1")
        completed, payload = self.invoke("claude")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "auth_expired")
        self.assertFalse(payload["retryable"])

    def test_codex_host_grok_requires_explicit_escalation(self):
        self.fake("grok", "printf 'SHOULD_NOT_RUN\\n'")
        completed, payload = self.invoke("grok", "--host", "codex")
        self.assertEqual(completed.returncode, 77)
        self.assertEqual(payload["status"], "needs_host_escalation")

    def test_codex_host_agy_work_requires_explicit_escalation(self):
        self.fake("agy", "printf 'SHOULD_NOT_RUN\\n'")
        completed, payload = self.invoke("agy", "--host", "codex", "--profile", "work")
        self.assertEqual(completed.returncode, 77)
        self.assertEqual(payload["status"], "needs_host_escalation")

    def test_nested_codex_runtime_failure_is_classified_as_sandbox_conflict(self):
        self.fake(
            "codex",
            "printf 'failed to initialize in-process app-server client: Operation not permitted (os error 1)\\n' >&2; exit 1",
        )
        completed, payload = self.invoke("codex")
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(payload["status"], "sandbox_conflict")
        self.assertTrue(payload["retryable"])
        self.assertTrue(payload["requires_host_escalation"])

    def test_resume_uses_registry_session(self):
        self.fake("grok", "printf '%s\\n' \"$*\"")
        first, created = self.invoke("grok", "--mode", "fresh", "--run-id", "same-topic")
        self.assertEqual(first.returncode, 0)
        session_id = created["session_id"]
        second, resumed = self.invoke("grok", "--mode", "resume", "--run-id", "same-topic")
        self.assertEqual(second.returncode, 0)
        self.assertEqual(resumed["session_id"], session_id)
        self.assertIn(f"--resume {session_id}", resumed["stdout"])

    def test_implicit_resume_rejects_profile_change(self):
        self.fake("grok", "printf 'GROK_OK\\n'")
        first, _ = self.invoke("grok", "--mode", "fresh", "--run-id", "profile-topic", "--profile", "review")
        self.assertEqual(first.returncode, 0)
        second, payload = self.invoke(
            "grok", "--mode", "resume", "--run-id", "profile-topic", "--profile", "work"
        )
        self.assertEqual(second.returncode, 65)
        self.assertEqual(payload["status"], "profile_mismatch")
        self.assertEqual(payload["previous_profile"], "review")

    def test_agy_never_uses_dangerous_skip(self):
        self.fake("agy", "printf '%s\\n' \"$*\"")
        completed, payload = self.invoke("agy")
        self.assertEqual(completed.returncode, 0)
        self.assertNotIn("dangerously-skip-permissions", payload["stdout"])
        self.assertIn("--mode plan", payload["stdout"])
        self.assertIn("--sandbox", payload["stdout"])
        self.assertIn(f"--add-dir {self.base.resolve()}", payload["stdout"])
        self.assertNotIn("Give an independent view.", payload["stdout"])
        self.assertIn("agy-brief.md", payload["stdout"])

    def test_successful_agy_internal_logs_are_suppressed_and_email_is_redacted(self):
        self.fake(
            "agy",
            "printf 'AGY_OK\\n'; printf 'I0716 12:00:00 internal user@example.com\\n' >&2",
        )
        completed, payload = self.invoke("agy")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertNotIn("user@example.com", payload["stderr_sanitized"])
        self.assertNotIn("internal", payload["stderr_sanitized"])
        self.assertIn("suppressed 1 provider diagnostic lines", payload["stderr_sanitized"])

    def test_missing_timeout_config_defaults_to_no_hard_timeout(self):
        self.fake("claude", "sleep 0.2; printf 'NO_TIMEOUT_OK\\n'")
        config = json.loads((ROOT / "backends.json").read_text(encoding="utf-8"))
        config["defaults"].pop("timeout_seconds", None)
        config_path = self.base / "backends-no-timeout.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        env = self.environment()
        env["CROSSCREW_BACKENDS_CONFIG"] = str(config_path)
        completed = subprocess.run(
            ["python3", str(SCRIPT), "claude", str(self.brief), "--target", str(self.base)],
            text=True,
            capture_output=True,
            env=env,
            timeout=5,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("NO_TIMEOUT_OK", payload["stdout"])

    def test_stale_registry_resume_is_rejected(self):
        self.fake("grok", "printf 'GROK_OK\\n'")
        first, created = self.invoke("grok", "--mode", "fresh", "--run-id", "stale-topic")
        self.assertEqual(first.returncode, 0)
        registry_path = self.state / "runs" / "stale-topic.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["providers"]["grok"]["updated_at"] = "2000-01-01T00:00:00+00:00"
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        second, payload = self.invoke("grok", "--mode", "resume", "--run-id", "stale-topic")
        self.assertEqual(second.returncode, 65)
        self.assertEqual(payload["status"], "session_stale")
        self.assertEqual(payload["session_id"], created["session_id"])

    def test_launch_oserror_returns_json_envelope(self):
        path = self.bin / "claude"
        path.write_text("not an executable format", encoding="utf-8")
        path.chmod(0o755)
        completed, payload = self.invoke("claude")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "error")
        self.assertIn("launch_error", payload["stderr_sanitized"])

    def test_state_files_are_private(self):
        self.fake("claude", "printf 'PRIVATE_OK\\n'")
        completed, _ = self.invoke("claude", "--run-id", "private-topic")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        registry = self.state / "runs" / "private-topic.json"
        self.assertEqual(registry.stat().st_mode & 0o777, 0o600)

    def test_parallel_providers_preserve_both_registry_entries(self):
        self.fake("claude", "sleep 0.2; printf 'CLAUDE_OK\\n'")
        self.fake("grok", "sleep 0.2; printf 'GROK_OK\\n'")
        base = ["python3", str(SCRIPT)]
        common = [str(self.brief), "--target", str(self.base), "--mode", "fresh", "--run-id", "parallel-topic"]
        processes = [
            subprocess.Popen(base + ["claude", *common], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.environment()),
            subprocess.Popen(base + ["grok", *common], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.environment()),
        ]
        for process in processes:
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0)
        registry = json.loads((self.state / "runs" / "parallel-topic.json").read_text(encoding="utf-8"))
        self.assertEqual(set(registry["providers"]), {"claude", "grok"})

    def test_same_provider_session_conflict_is_rejected(self):
        held = self.base / "first-held"
        release = self.base / "first-release"
        self.fake(
            "grok",
            f"printf held > '{held}'; "
            f"while [ ! -f '{release}' ]; do sleep 0.05; done; "
            "printf 'FIRST_OK\\n'",
        )
        command = [
            "python3",
            str(SCRIPT),
            "grok",
            str(self.brief),
            "--target",
            str(self.base),
            "--mode",
            "fresh",
            "--session-id",
            "shared-session",
            "--run-id",
            "shared-run",
        ]
        first = subprocess.Popen(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.environment()
        )
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not held.exists():
                if first.poll() is not None:
                    stdout, stderr = first.communicate()
                    self.fail(
                        "first process exited before holding the session lock: "
                        f"{first.returncode} {stdout} {stderr}"
                    )
                time.sleep(0.02)
            self.assertTrue(
                held.exists(),
                "first process did not reach the provider with the session lock held",
            )
            second = subprocess.run(
                command, text=True, capture_output=True, env=self.environment(), timeout=5
            )
            payload = json.loads(second.stdout)
            self.assertEqual(second.returncode, 75)
            self.assertEqual(payload["status"], "conflict")
        finally:
            release.write_text("go", encoding="utf-8")
            first.communicate(timeout=5)
        self.assertEqual(first.returncode, 0)

    def test_codex_fresh_extracts_session_and_resume_reuses_it(self):
        self.fake(
            "codex",
            "out=''; while [ $# -gt 0 ]; do if [ \"$1\" = '-o' ]; then shift; out=$1; fi; shift; done; "
            "printf 'CODEX_LAST\\n' > \"$out\"; "
            "printf '{\"type\":\"thread.started\",\"thread_id\":\"codex-thread-1\"}\\n'",
        )
        first, fresh = self.invoke("codex", "--mode", "fresh", "--run-id", "codex-resume")
        self.assertEqual(first.returncode, 0)
        self.assertEqual(fresh["session_id"], "codex-thread-1")
        self.assertIn("CODEX_LAST", fresh["stdout"])
        second, resumed = self.invoke("codex", "--mode", "resume", "--run-id", "codex-resume")
        self.assertEqual(second.returncode, 0)
        self.assertEqual(resumed["session_id"], "codex-thread-1")

    def test_session_expired_rehydrates_once_from_transcript(self):
        self.fake(
            "grok",
            "case \" $* \" in *' --resume old-session '*) printf 'session expired\\n' >&2; exit 1;; "
            "*) printf 'REHYDRATED_OK\\n';; esac",
        )
        transcript = self.base / "transcript.md"
        transcript.write_text("Previous useful context.", encoding="utf-8")
        completed, payload = self.invoke(
            "grok",
            "--mode",
            "resume",
            "--session-id",
            "old-session",
            "--run-id",
            "rehydrate-run",
            "--rehydrate-file",
            str(transcript),
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["rehydrated"])
        self.assertEqual(payload["replaced_session_id"], "old-session")
        self.assertIn("REHYDRATED_OK", payload["stdout"])

    def test_shell_wrappers_exist_and_are_executable(self):
        for name in ("call_worker.sh", "worker_job.sh"):
            path = ROOT / name
            self.assertTrue(path.is_file())
            self.assertTrue(os.access(path, os.X_OK))

    def test_known_startup_noise_is_suppressed(self):
        self.fake(
            "grok",
            "printf 'RESULT\\n'; printf '\\033[33mWARN\\033[0m plugin name collision resolved by scope precedence\\n' >&2",
        )
        completed, payload = self.invoke("grok")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("suppressed 1 known startup warning", payload["stderr_sanitized"])
        self.assertNotIn("scope precedence", payload["stderr_sanitized"])


if __name__ == "__main__":
    unittest.main()
