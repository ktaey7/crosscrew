import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
JOB = ROOT / "worker_job.py"


class WorkerJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.state = self.base / "state"
        self.brief = self.base / "brief.md"
        self.brief.write_text("Return a test result.", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def fake(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(0o755)

    def fake_claude(self, body):
        self.fake("claude", body)

    def environment(self):
        env = os.environ.copy()
        for name in ("CROSSCREW_SUPERVISED", "CROSSCREW_LAUNCHER_SIDECAR", "CROSSCREW_LAUNCHER_TOKEN"):
            env.pop(name, None)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["CROSSCREW_STATE_DIR"] = str(self.state)
        env["CROSSCREW_AGY_PROJECTS_DIR"] = str(self.base / "agy-projects")
        env["HOME"] = str(self.base)
        return env

    def invoke(self, *args, expected=None):
        completed = subprocess.run(
            ["python3", str(JOB), *args],
            text=True,
            capture_output=True,
            env=self.environment(),
            timeout=10,
        )
        if expected is not None:
            self.assertEqual(completed.returncode, expected, completed.stderr)
        return completed, json.loads(completed.stdout)

    def wait_until_done(self, job_id):
        deadline = time.time() + 5
        while time.time() < deadline:
            _, status = self.invoke("status", job_id, expected=0)
            if status["status"] != "running":
                return status
            time.sleep(0.05)
        self.fail("job did not finish")

    def process_alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def process_group_alive(self, pgid):
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False

    def test_start_returns_immediately_and_collects_result(self):
        self.fake_claude("sleep 0.4; printf 'JOB_OK\\n'")
        started_at = time.monotonic()
        _, started = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--job-id",
            "job-success",
            expected=0,
        )
        self.assertLess(time.monotonic() - started_at, 0.3)
        self.assertEqual(started["status"], "running")
        self.assertIsNone(started["hard_timeout_seconds"])
        final_status = self.wait_until_done("job-success")
        self.assertEqual(final_status["status"], "completed")
        self.assertLess(final_status["duration_seconds"], 2)
        _, result = self.invoke("collect", "job-success", expected=0)
        self.assertEqual(result["status"], "ok")
        self.assertIn("JOB_OK", result["stdout"])

    def test_work_profile_is_propagated_to_job_metadata_and_result(self):
        self.fake_claude("printf 'WORK_JOB_OK\\n'")
        _, started = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--profile",
            "work",
            "--job-id",
            "job-work-profile",
            expected=0,
        )
        self.assertEqual(started["profile"], "work")
        meta = json.loads((self.state / "jobs" / "job-work-profile" / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["profile"], "work")
        final_status = self.wait_until_done("job-work-profile")
        self.assertEqual(final_status["profile"], "work")
        _, result = self.invoke("collect", "job-work-profile", expected=0)
        self.assertEqual(result["profile"], "work")
        self.assertEqual(result["write_policy"], "prompt_guarded_target_write")

    def test_claude_model_override_is_propagated_to_worker(self):
        self.fake_claude("printf '%s\n' \"$*\"")
        _, started = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--model",
            "opus",
            "--job-id",
            "job-claude-opus",
            expected=0,
        )
        self.assertEqual(started["model"], "opus")
        meta = json.loads((self.state / "jobs" / "job-claude-opus" / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["model"], "opus")
        self.wait_until_done("job-claude-opus")
        _, result = self.invoke("collect", "job-claude-opus", expected=0)
        self.assertEqual(result["model"], "opus")
        self.assertIn("--model opus", result["stdout"])

    def test_media_job_propagates_contract_and_collects_verified_artifact(self):
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
        output_dir = self.base / "media-output"
        _, started = self.invoke(
            "start",
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
            str(output_dir),
            "--job-id",
            "job-grok-media",
            expected=0,
        )
        self.assertEqual(started["media_kind"], "image")
        self.assertEqual(Path(started["output_dir"]), output_dir.resolve())
        self.wait_until_done("job-grok-media")
        _, result = self.invoke("collect", "job-grok-media", expected=0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["media_kind"], "image")
        self.assertEqual(len(result["artifacts"]), 1)

    def test_video_job_fails_before_spawn_when_personal_privacy_needs_output(self):
        self.fake("grok", "printf 'SHOULD_NOT_RUN\\n'")
        grok_home = self.base / ".grok"
        grok_home.mkdir()
        (grok_home / "auth.json").write_text(
            json.dumps(
                {
                    "scope": {
                        "auth_mode": "oidc",
                        "principal_type": "User",
                        "team_blocked_reasons": [],
                        "coding_data_retention_opt_out": True,
                    }
                }
            ),
            encoding="utf-8",
        )
        _, payload = self.invoke(
            "start",
            "grok",
            str(self.brief),
            "--target",
            str(self.base),
            "--profile",
            "media",
            "--mode",
            "fresh",
            "--media-kind",
            "video",
            "--output-dir",
            str(self.base / "media-output"),
            expected=78,
        )
        self.assertEqual(payload["status"], "zdr_output_required")
        self.assertEqual(payload["privacy_scope"], "coding_data_opt_out")
        self.assertFalse(payload["private_output_configured"])
        self.assertEqual(list((self.state / "jobs").iterdir()), [])

    def test_agy_work_profile_is_propagated_and_project_grant_is_cleaned(self):
        self.fake("agy", "printf 'AGY_WORK_JOB_OK\\n'")
        _, started = self.invoke(
            "start",
            "agy",
            str(self.brief),
            "--target",
            str(self.base),
            "--profile",
            "work",
            "--job-id",
            "job-agy-work",
            expected=0,
        )
        self.assertEqual(started["profile"], "work")
        final_status = self.wait_until_done("job-agy-work")
        self.assertEqual(final_status["status"], "completed")
        _, result = self.invoke("collect", "job-agy-work", expected=0)
        self.assertEqual(result["profile"], "work")
        self.assertEqual(result["write_policy"], "os_sandbox_project_grant_target_write")
        self.assertIn("AGY_WORK_JOB_OK", result["stdout"])
        self.assertEqual(list((self.base / "agy-projects").glob("multi-ai-work-*.json")), [])
        self.assertEqual(list((self.base / "agy-projects").glob("multi-ai-work-*.lock")), [])

    def test_agy_work_project_grant_is_cleaned_on_explicit_cancel(self):
        self.fake("agy", "sleep 30")
        _, started = self.invoke(
            "start",
            "agy",
            str(self.brief),
            "--target",
            str(self.base),
            "--profile",
            "work",
            "--job-id",
            "job-agy-work-cancel",
            expected=0,
        )
        projects = self.base / "agy-projects"
        deadline = time.time() + 3
        while time.time() < deadline and not list(projects.glob("multi-ai-work-*.json")):
            time.sleep(0.05)
        self.assertTrue(list(projects.glob("multi-ai-work-*.json")))
        _, canceled = self.invoke("cancel", started["job_id"], expected=0)
        self.assertEqual(canceled["status"], "canceled")
        self.assertEqual(list(projects.glob("multi-ai-work-*.json")), [])
        self.assertEqual(list(projects.glob("multi-ai-work-*.lock")), [])

    def test_agy_orphan_grant_after_sigkill_is_reclaimed_by_purge(self):
        self.fake("agy", "sleep 30")
        _, started = self.invoke(
            "start", "agy", str(self.brief), "--target", str(self.base),
            "--profile", "work", "--job-id", "job-agy-sigkill", expected=0,
        )
        projects = self.base / "agy-projects"
        deadline = time.time() + 3
        while time.time() < deadline and not list(projects.glob("multi-ai-work-*.json")):
            time.sleep(0.05)
        self.assertTrue(list(projects.glob("multi-ai-work-*.json")))
        os.killpg(started["pgid"], signal.SIGKILL)
        deadline = time.time() + 3
        while time.time() < deadline:
            _, status = self.invoke("status", started["job_id"], expected=0)
            if status["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(status["status"], "failed")
        self.assertTrue(list(projects.glob("multi-ai-work-*.json")))
        _, purged = self.invoke("purge", "--older-than-days", "0", expected=0)
        self.assertTrue(purged["agy_orphan_grants_removed"])
        self.assertEqual(list(projects.glob("multi-ai-work-*")), [])

    def test_collect_while_running_returns_conflict(self):
        self.fake_claude("sleep 30")
        _, started = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--job-id",
            "job-collect-running",
            expected=0,
        )
        _, payload = self.invoke("collect", started["job_id"], expected=75)
        self.assertEqual(payload["status"], "running")
        self.invoke("cancel", started["job_id"], expected=0)

    def test_launcher_abnormal_exit_without_result_is_failed(self):
        self.fake_claude("sleep 30")
        _, started = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--job-id",
            "job-launcher-killed",
            expected=0,
        )
        os.killpg(started["pgid"], signal.SIGKILL)
        deadline = time.time() + 3
        status = None
        while time.time() < deadline:
            _, status = self.invoke("status", started["job_id"], expected=0)
            if status["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(status["status"], "failed")
        self.assertIn("without a valid result envelope", status["error"])

    def test_check_after_marks_attention_without_canceling(self):
        self.fake_claude("sleep 5; printf 'LATE_OK\\n'")
        _, started = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--job-id",
            "job-attention",
            "--check-after",
            "0.1",
            expected=0,
        )
        time.sleep(0.2)
        _, status = self.invoke("status", "job-attention", expected=0)
        self.assertEqual(status["status"], "running")
        self.assertTrue(status["needs_attention"])
        self.assertFalse(status["auto_canceled"])
        _, canceled = self.invoke("cancel", "job-attention", expected=0)
        self.assertEqual(canceled["status"], "canceled")
        self.assertEqual(canceled["reason"], "explicit_cancel")

    def test_cancel_kills_launcher_provider_and_grandchild(self):
        provider_pid = self.base / "provider.pid"
        child_pid = self.base / "child.pid"
        self.fake_claude(
            f"echo $$ > '{provider_pid}'; sleep 30 & child=$!; echo $child > '{child_pid}'; wait"
        )
        _, started = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--job-id",
            "job-tree-cancel",
            expected=0,
        )
        deadline = time.time() + 3
        while time.time() < deadline and (not provider_pid.exists() or not child_pid.exists()):
            time.sleep(0.05)
        self.assertTrue(provider_pid.exists())
        self.assertTrue(child_pid.exists())
        _, canceled = self.invoke("cancel", "job-tree-cancel", expected=0)
        self.assertEqual(canceled["status"], "canceled")
        deadline = time.time() + 3
        while time.time() < deadline and self.process_group_alive(started["pgid"]):
            time.sleep(0.05)
        self.assertFalse(self.process_group_alive(started["pgid"]))
        completed, collected = self.invoke("collect", "job-tree-cancel", expected=130)
        self.assertEqual(collected["status"], "canceled")

    def test_cancel_after_completion_does_not_overwrite_terminal_state(self):
        self.fake_claude("printf 'DONE_FIRST\\n'")
        _, started = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--job-id",
            "job-complete-before-cancel",
            expected=0,
        )
        self.wait_until_done(started["job_id"])
        _, canceled = self.invoke("cancel", started["job_id"], expected=0)
        self.assertEqual(canceled["status"], "already_completed")
        self.assertEqual(canceled["prior_status"], "completed")
        self.assertFalse((self.state / "jobs" / started["job_id"] / "canceled.json").exists())
        _, result = self.invoke("collect", started["job_id"], expected=0)
        self.assertIn("DONE_FIRST", result["stdout"])

    def test_cancel_fails_closed_on_identity_mismatch(self):
        self.fake_claude("sleep 30")
        _, started = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--job-id",
            "job-stale-pid",
            expected=0,
        )
        meta_path = self.state / "jobs" / started["job_id"] / "job.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        original = dict(meta)
        meta["launcher_token"] = "definitely-not-the-real-launcher-token"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        _, payload = self.invoke("cancel", started["job_id"], expected=75)
        self.assertEqual(payload["status"], "cancel_unverified")
        self.assertTrue(self.process_group_alive(started["pgid"]))
        meta_path.write_text(json.dumps(original), encoding="utf-8")
        _, canceled = self.invoke("cancel", started["job_id"], expected=0)
        self.assertEqual(canceled["status"], "canceled")

    def test_resume_job_metadata_contains_registry_session(self):
        self.fake_claude("printf 'SESSION_OK\\n'")
        _, first = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--mode",
            "fresh",
            "--run-id",
            "resume-metadata",
            "--job-id",
            "resume-fresh",
            expected=0,
        )
        self.wait_until_done(first["job_id"])
        _, second = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--mode",
            "resume",
            "--run-id",
            "resume-metadata",
            "--job-id",
            "resume-followup",
            expected=0,
        )
        self.assertEqual(second["session_id"], first["session_id"])
        self.wait_until_done(second["job_id"])

    def test_supervisor_resume_does_not_bypass_stale_registry_guard(self):
        self.fake_claude("printf 'SESSION_OK\\n'")
        _, first = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--mode",
            "fresh",
            "--run-id",
            "stale-supervisor",
            "--job-id",
            "stale-fresh",
            expected=0,
        )
        self.wait_until_done(first["job_id"])
        registry_path = self.state / "runs" / "stale-supervisor.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["providers"]["claude"]["updated_at"] = "2000-01-01T00:00:00+00:00"
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        _, second = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--mode",
            "resume",
            "--run-id",
            "stale-supervisor",
            "--job-id",
            "stale-resume",
            expected=0,
        )
        final = self.wait_until_done(second["job_id"])
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["worker_status"], "session_stale")

    def test_legacy_completed_job_without_identity_token_still_collects(self):
        self.fake_claude("printf 'LEGACY_OK\\n'")
        _, started = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--job-id",
            "job-legacy-completed",
            expected=0,
        )
        self.wait_until_done(started["job_id"])
        meta_path = self.state / "jobs" / started["job_id"] / "job.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        for key in ("launcher_token", "sidecar_path", "lock_path"):
            meta.pop(key, None)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        _, status = self.invoke("status", started["job_id"], expected=0)
        self.assertEqual(status["status"], "completed")
        _, result = self.invoke("collect", started["job_id"], expected=0)
        self.assertIn("LEGACY_OK", result["stdout"])

    def test_zero_check_after_is_preserved_and_labeled_weak(self):
        self.fake_claude("sleep 30")
        _, started = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--job-id",
            "job-zero-check",
            "--check-after",
            "0",
            expected=0,
        )
        _, status = self.invoke("status", started["job_id"], expected=0)
        self.assertEqual(status["check_after_seconds"], 0)
        self.assertTrue(status["needs_attention"])
        self.assertEqual(status["attention_signal"], "weak")
        self.invoke("cancel", started["job_id"], expected=0)

    def test_job_state_permissions_are_private(self):
        self.fake_claude("printf 'PRIVATE_JOB\\n'")
        _, started = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--job-id",
            "job-private",
            expected=0,
        )
        directory = self.state / "jobs" / started["job_id"]
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for name in ("brief.md", "job.json", "result.pending.json", "launcher.stderr"):
            self.assertEqual((directory / name).stat().st_mode & 0o777, 0o600)
        self.wait_until_done(started["job_id"])

    def test_status_recovers_launcher_from_child_sidecar(self):
        self.fake_claude("sleep 30")
        _, started = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--job-id",
            "job-sidecar-recovery",
            expected=0,
        )
        meta_path = self.state / "jobs" / started["job_id"] / "job.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update({"state": "spawning", "pid": None, "pgid": None})
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        _, status = self.invoke("status", started["job_id"], expected=0)
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["pid"], started["pid"])
        recovered = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertTrue(recovered["recovered_from_sidecar"])
        self.invoke("cancel", started["job_id"], expected=0)

    def test_purge_removes_only_terminal_jobs(self):
        self.fake_claude("printf 'TERMINAL\\n'")
        _, terminal = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--job-id",
            "job-purge-terminal",
            expected=0,
        )
        self.wait_until_done(terminal["job_id"])
        self.fake_claude("sleep 30")
        _, active = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--job-id",
            "job-purge-active",
            expected=0,
        )
        _, purged = self.invoke("purge", "--older-than-days", "0", expected=0)
        self.assertIn(terminal["job_id"], purged["removed_jobs"])
        self.assertIn(active["job_id"], purged["skipped_active_jobs"])
        self.assertFalse((self.state / "jobs" / terminal["job_id"]).exists())
        self.assertTrue((self.state / "jobs" / active["job_id"]).exists())
        self.invoke("cancel", active["job_id"], expected=0)

    def test_brief_is_copied_into_job_directory(self):
        self.fake_claude("sleep 0.2; printf 'COPIED\\n'")
        _, started = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--target",
            str(self.base),
            "--job-id",
            "job-copy",
            expected=0,
        )
        self.brief.unlink()
        self.wait_until_done(started["job_id"])
        _, result = self.invoke("collect", started["job_id"], expected=0)
        self.assertEqual(result["status"], "ok")

    def test_codex_host_grok_escalation_is_reported_before_spawn(self):
        completed, payload = self.invoke(
            "start",
            "grok",
            str(self.brief),
            "--host",
            "codex",
            "--target",
            str(self.base),
            expected=77,
        )
        self.assertEqual(payload["status"], "needs_host_escalation")
        self.assertFalse(any((self.state / "jobs").iterdir()))

    def test_codex_host_claude_fresh_escalation_is_reported_before_spawn(self):
        completed, payload = self.invoke(
            "start",
            "claude",
            str(self.brief),
            "--host",
            "codex",
            "--target",
            str(self.base),
            "--mode",
            "fresh",
            expected=77,
        )
        self.assertEqual(payload["status"], "needs_host_escalation")
        self.assertFalse(any((self.state / "jobs").iterdir()))


if __name__ == "__main__":
    unittest.main()
