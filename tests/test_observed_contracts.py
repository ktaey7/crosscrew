"""Regressions for two live delegation-contract mismatches.

Observation A: brokered `worker_job start` printed JSON status=running with a
valid job_id/session_id, but the shell exit was 1. Acceptance is not worker
completion and is not a start rejection.

Observation B: agy research/oneshot --check was ready, then the real run
returned provider exit 0, empty stdout, a headless read_url auto-deny on
stderr, and empty_output/retryable true. That is not generic empty output,
and --check does not verify web reads or tool permissions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import call_worker  # noqa: E402
import worker_job  # noqa: E402

CALL_WORKER = ROOT / "call_worker.py"

# Inspection fixture copied from the live agy stderr. Not an execution instruction.
JETSKI_READ_URL_DENIED = (
    'jetski: no output produced — a tool required the "read_url" permission '
    "that headless mode cannot prompt for, so it was auto-denied. Add an allow-rule "
    "under permissions.allow in settings.json (e.g. read_url(<target>)). Alternatively, "
    "re-run with --dangerously-skip-permissions to auto-approve all tools."
)


class StartAcceptanceExitTests(unittest.TestCase):
    def test_observed_running_envelope_is_accepted(self):
        payload = {
            "schema_version": "1.0",
            "status": "running",
            "job_id": "claude-20260912-120000-abc123",
            "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "provider": "claude",
            "profile": "review",
        }
        self.assertEqual(worker_job.start_shell_exit(payload), 0)

    def test_running_without_job_id_is_not_acceptance(self):
        self.assertEqual(worker_job.start_shell_exit({"status": "running"}), 1)

    def test_spawn_failed_keeps_nonzero_exit(self):
        self.assertEqual(
            worker_job.start_shell_exit(
                {"status": "spawn_failed", "job_id": "j-spawn", "exit_code": 71}
            ),
            71,
        )

    def test_start_rejection_keeps_its_exit(self):
        self.assertEqual(
            worker_job.start_shell_exit(
                {"status": "needs_host_escalation", "exit_code": 77}
            ),
            77,
        )
        self.assertEqual(
            worker_job.start_shell_exit({"status": "invalid_input", "exit_code": 66}),
            66,
        )
        self.assertEqual(
            worker_job.start_shell_exit({"status": "self_delegation", "exit_code": 64}),
            64,
        )


class StartJobBrokerMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.brief = self.base / "brief.md"
        self.brief.write_text("task", encoding="utf-8")
        self.state = self.base / "state"
        self.state.mkdir()
        self.env = patch.dict(os.environ, {"CROSSCREW_STATE_DIR": str(self.state)}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _args(self, job_id):
        return worker_job.parser().parse_args(
            [
                "start",
                "claude",
                str(self.brief),
                "--host",
                "codex",
                "--target",
                str(self.base),
                "--profile",
                "review",
                "--mode",
                "fresh",
                "--job-id",
                job_id,
            ]
        )

    def test_brokered_running_envelope_exits_zero(self):
        payload = {
            "schema_version": "1.0",
            "status": "running",
            "job_id": "j-observed",
            "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "provider": "claude",
            "profile": "review",
        }
        with patch.object(worker_job.broker_client, "request", return_value=payload):
            with patch.object(worker_job, "print_json") as printed:
                code = worker_job.start_job(self._args("j-observed"), worker_job.load_config(), self.state)
        self.assertEqual(code, 0)
        printed.assert_called_once()
        self.assertEqual(printed.call_args[0][0]["status"], "running")
        self.assertEqual(printed.call_args[0][0]["job_id"], "j-observed")

    def test_brokered_spawn_failed_is_not_rewritten_to_success(self):
        payload = {
            "schema_version": "1.0",
            "status": "spawn_failed",
            "job_id": "j-spawn",
            "exit_code": 71,
            "error": "spawn exploded",
        }
        with patch.object(worker_job.broker_client, "request", return_value=payload):
            with patch.object(worker_job, "print_json"):
                code = worker_job.start_job(self._args("j-spawn"), worker_job.load_config(), self.state)
        self.assertEqual(code, 71)


class HeadlessToolPermissionTests(unittest.TestCase):
    def test_headless_read_url_deny_is_not_retryable_empty_output(self):
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                status, retryable = call_worker.classify(returncode, "", JETSKI_READ_URL_DENIED, False)
                self.assertEqual(status, "tool_permission_denied")
                self.assertFalse(retryable)

    def test_generic_empty_output_stays_retryable(self):
        status, retryable = call_worker.classify(0, "", "provider finished with no body", False)
        self.assertEqual(status, "empty_output")
        self.assertTrue(retryable)

    def test_auth_failure_mentioning_permission_is_not_the_new_status(self):
        status, retryable = call_worker.classify(
            1,
            "",
            "Authentication expired: permission denied while refreshing token\n",
            False,
        )
        self.assertEqual(status, "auth_expired")
        self.assertFalse(retryable)

    def test_mixed_auth_and_headless_deny_stays_auth_expired(self):
        # Independent synthetic case: auth + jetski on a non-zero exit.
        # The new tool status must not shadow the existing auth classifier.
        stderr = (
            "Authentication failed\n"
            "jetski: no output produced — a tool required the \"read_url\" "
            "permission that headless mode cannot prompt for, so it was auto-denied."
        )
        status, retryable = call_worker.classify(1, "", stderr, False)
        self.assertEqual(status, "auth_expired")
        self.assertFalse(retryable)

    def test_successful_stdout_discussing_permission_stays_ok(self):
        status, retryable = call_worker.classify(
            0,
            'Grant the read_url permission only after reviewing the target.\n',
            "",
            False,
        )
        self.assertEqual(status, "ok")
        self.assertFalse(retryable)

    def test_nonzero_permission_denied_stays_blocked(self):
        status, retryable = call_worker.classify(1, "", "permission denied", False)
        self.assertEqual(status, "blocked")
        self.assertFalse(retryable)

    def _unique_stderr_prefix(self, n=500, width=80):
        return "\n".join(f"unique diagnostic line {i:04d} " + ("x" * width) for i in range(n))

    def test_long_unique_stderr_keeps_headless_deny_classification(self):
        raw = self._unique_stderr_prefix() + "\n" + JETSKI_READ_URL_DENIED
        self.assertGreater(len(raw.encode("utf-8")), 32768)
        prepared = call_worker.prepare_stderr(raw)
        displayed = call_worker.sanitize_stderr(raw, 32768)
        status, retryable = call_worker.classify(
            0, "", displayed, False, tool_denied=call_worker._headless_tool_auto_denied(prepared)
        )
        self.assertEqual(status, "tool_permission_denied")
        self.assertFalse(retryable)
        self.assertIn("...[truncated]", displayed)
        self.assertLessEqual(len(displayed.encode("utf-8")), 32768 + len(b"\n...[truncated]"))

    def test_stderr_evidence_obeys_small_and_unicode_byte_budgets(self):
        raw = "\n".join("가나다라마바사" + chr(0x4e00 + i) * 60 for i in range(400))
        raw += "\napi_key=synthetic_secret\n" + JETSKI_READ_URL_DENIED
        for limit in (0, 1, 10, 32, 32768):
            with self.subTest(limit=limit):
                displayed = call_worker.sanitize_stderr(raw, limit)
                self.assertLessEqual(len(displayed.encode("utf-8")), limit + len(b"\n...[truncated]"))
                self.assertNotIn("synthetic_secret", displayed)
                self.assertNotIn("\ufffd", displayed)

    def test_numeric_identifier_is_not_http_auth_failure(self):
        for diagnostic in ("request 00401", "job 140123", "task id401suffix"):
            with self.subTest(diagnostic=diagnostic):
                stderr = diagnostic + "\n" + JETSKI_READ_URL_DENIED
                self.assertEqual(call_worker.classify(1, "", stderr, False), ("tool_permission_denied", False))
        for diagnostic in ("HTTP 401", "status=401", "401 Unauthorized"):
            with self.subTest(diagnostic=diagnostic):
                self.assertEqual(call_worker.classify(1, "", diagnostic, False), ("auth_expired", False))

    def test_duplicate_stderr_is_classified_and_stays_compact(self):
        raw = "\n".join(["duplicate diagnostic line without secrets"] * 4000)
        raw = raw + "\n" + JETSKI_READ_URL_DENIED
        self.assertGreater(len(raw.encode("utf-8")), 32768)
        prepared = call_worker.prepare_stderr(raw)
        displayed = call_worker.sanitize_stderr(raw, 32768)
        status, retryable = call_worker.classify(
            0, "", displayed, False, tool_denied=call_worker._headless_tool_auto_denied(prepared)
        )
        self.assertEqual(status, "tool_permission_denied")
        self.assertFalse(retryable)
        self.assertNotIn("...[truncated]", displayed)
        self.assertIn("headless mode cannot prompt", displayed)
        self.assertLess(len(displayed.encode("utf-8")), 1024)

    def test_long_unique_stderr_does_not_shadow_auth_or_ok_stdout(self):
        deny_tail = self._unique_stderr_prefix() + "\n" + JETSKI_READ_URL_DENIED
        auth_stderr = "Authentication expired: token refresh failed\n" + deny_tail
        prepared_auth = call_worker.prepare_stderr(auth_stderr)
        status, retryable = call_worker.classify(1, "", prepared_auth, False)
        self.assertEqual(status, "auth_expired")
        self.assertFalse(retryable)
        status, retryable = call_worker.classify(
            0,
            "Grant the read_url permission only after reviewing the target.\n",
            call_worker.prepare_stderr(deny_tail),
            False,
        )
        self.assertEqual(status, "ok")
        self.assertFalse(retryable)

    def test_long_stderr_redacts_secrets_before_truncation(self):
        raw = (
            "contact user@example.com sk-abcdefghijklmnopqrstuvwxyz1234\n"
            + self._unique_stderr_prefix()
            + "\n"
            + JETSKI_READ_URL_DENIED
            + " contact user@example.com sk-abcdefghijklmnopqrstuvwxyz1234"
        )
        displayed = call_worker.sanitize_stderr(raw, 32768)
        prepared = call_worker.prepare_stderr(raw)
        self.assertIn("[REDACTED_EMAIL]", displayed)
        self.assertIn("[REDACTED_TOKEN]", displayed)
        self.assertNotIn("user@example.com", displayed)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz1234", displayed)
        self.assertNotIn("user@example.com", prepared)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz1234", prepared)


class AdapterObservedContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.state = self.base / "state"
        self.brief = self.base / "brief.md"
        self.brief.write_text("Research the public docs and return sources.", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def fake(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(0o755)

    def environment(self):
        env = os.environ.copy()
        for name in (
            "CROSSCREW_SUPERVISED",
            "CROSSCREW_LAUNCHER_SIDECAR",
            "CROSSCREW_LAUNCHER_TOKEN",
            "CROSSCREW_BROKER_CHILD",
        ):
            env.pop(name, None)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["CROSSCREW_STATE_DIR"] = str(self.state)
        env["CROSSCREW_AGY_PROJECTS_DIR"] = str(self.base / "agy-projects")
        env["HOME"] = str(self.base)
        return env

    def invoke(self, provider, *extra):
        completed = subprocess.run(
            ["python3", str(CALL_WORKER), provider, str(self.brief), "--target", str(self.base), *extra],
            text=True,
            capture_output=True,
            env=self.environment(),
            timeout=20,
        )
        return completed, json.loads(completed.stdout)

    def test_headless_read_url_deny_envelope_is_not_retryable_empty_output(self):
        # Live observation was agy research/oneshot. Classification is stderr-based;
        # claude is used here so this suite is not blocked by agy/seatbelt spawn.
        quoted = json.dumps(JETSKI_READ_URL_DENIED)
        self.fake("claude", f"printf '' ; printf '%s\\n' {quoted} >&2; exit 0")
        completed, payload = self.invoke("claude", "--profile", "research", "--mode", "oneshot")
        self.assertEqual(payload["status"], "tool_permission_denied", payload)
        self.assertFalse(payload["retryable"])
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("headless mode cannot prompt", payload["stderr_sanitized"])
        self.assertIn("do not retry", payload.get("hint", "").lower())
        self.assertNotIn("dangerously-skip-permissions", payload.get("hint", "").lower())

    def test_empty_stdout_without_headless_deny_stays_empty_output(self):
        self.fake("claude", "exit 0")
        completed, payload = self.invoke("claude")
        self.assertEqual(payload["status"], "empty_output", payload)
        self.assertTrue(payload["retryable"])
        self.assertNotEqual(completed.returncode, 0)

    def test_long_unique_stderr_envelope_is_not_retryable_empty_output(self):
        raw = "\n".join(f"unique diagnostic line {i:04d} " + ("x" * 80) for i in range(500))
        raw = raw + "\n" + JETSKI_READ_URL_DENIED
        errfile = self.base / "unique-stderr.txt"
        errfile.write_text(raw, encoding="utf-8")
        self.fake("claude", f"cat '{errfile}' >&2; exit 0")
        completed, payload = self.invoke("claude", "--profile", "research", "--mode", "oneshot")
        self.assertEqual(payload["status"], "tool_permission_denied", payload)
        self.assertFalse(payload["retryable"])
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("...[truncated]", payload["stderr_sanitized"])
        self.assertLessEqual(len(payload["stderr_sanitized"].encode("utf-8")), 32768 + len(b"\n...[truncated]"))
        self.assertIn("do not retry", payload.get("hint", "").lower())

    def test_duplicate_stderr_envelope_is_classified_and_compact(self):
        raw = "\n".join(["duplicate diagnostic line without secrets"] * 4000)
        raw = raw + "\n" + JETSKI_READ_URL_DENIED
        errfile = self.base / "duplicate-stderr.txt"
        errfile.write_text(raw, encoding="utf-8")
        self.fake("claude", f"cat '{errfile}' >&2; exit 0")
        completed, payload = self.invoke("claude", "--profile", "research", "--mode", "oneshot")
        self.assertEqual(payload["status"], "tool_permission_denied", payload)
        self.assertFalse(payload["retryable"])
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("...[truncated]", payload["stderr_sanitized"])
        self.assertIn("headless mode cannot prompt", payload["stderr_sanitized"])
        self.assertLess(len(payload["stderr_sanitized"].encode("utf-8")), 1024)

    def test_research_check_ready_does_not_claim_web_or_tool_verification(self):
        self.fake("agy", "printf 'SHOULD_NOT_RUN\\n'")
        completed = subprocess.run(
            [
                "python3",
                str(CALL_WORKER),
                "agy",
                "--host",
                "codex",
                "--profile",
                "research",
                "--mode",
                "oneshot",
                "--check",
            ],
            text=True,
            capture_output=True,
            env=self.environment(),
            timeout=20,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["check_scope"], "reachability")
        self.assertEqual(
            payload["check_unverified"],
            ["authentication", "tool_permissions", "web_reads"],
        )
        self.assertNotIn("SHOULD_NOT_RUN", json.dumps(payload))
        self.assertNotIn("web_ready", payload)
        self.assertNotIn("tool_permissions_ok", payload)


if __name__ == "__main__":
    unittest.main()
