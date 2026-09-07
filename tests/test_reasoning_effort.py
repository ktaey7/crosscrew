"""Reasoning-effort passthrough.

The registry declares which providers accept a reasoning effort and which values
are valid, so the dispatcher, the supervisor, and the broker cannot drift apart
on the answer. Requesting an effort a provider does not declare is a rejection,
never a silent downgrade to the provider's configured default.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
import sys  # noqa: E402

sys.path.insert(0, str(ROOT))

import broker_protocol as protocol  # noqa: E402
import effort_registry  # noqa: E402

CALL_WORKER = ROOT / "call_worker.py"
WORKER_JOB = ROOT / "worker_job.py"

# A fake `codex` that echoes its argv and stdin into the file given by -o.
CODEX_ECHO = (
    "args=\"$*\"; out=''; while [ $# -gt 0 ]; do if [ \"$1\" = '-o' ]; then shift; out=$1; fi; shift; done; "
    "body=$(cat); printf '%s\\n%s\\n' \"$args\" \"$body\" > \"$out\""
)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "backends.json").read_text(encoding="utf-8"))

    def test_codex_declares_the_current_model_delegation_allowlist(self):
        self.assertEqual(
            effort_registry.values(self.config, "codex"),
            ["low", "medium", "high", "xhigh", "max"],
        )

    def test_providers_without_a_declaration_accept_no_effort(self):
        for provider in ("claude", "grok", "agy"):
            self.assertEqual(effort_registry.values(self.config, provider), [], provider)

    def test_declared_value_validates(self):
        self.assertIsNone(effort_registry.validation_error(self.config, "codex", "max"))

    def test_undeclared_value_is_rejected_with_the_allowed_set(self):
        error = effort_registry.validation_error(self.config, "codex", "ultra")
        self.assertIsNotNone(error)
        status, hint = error
        self.assertEqual(status, "unsupported_effort")
        self.assertIn("max", hint)

    def test_provider_without_capability_is_rejected(self):
        error = effort_registry.validation_error(self.config, "claude", "max")
        self.assertIsNotNone(error)
        status, hint = error
        self.assertEqual(status, "unsupported_effort")
        self.assertIn("claude", hint)

    def test_absent_effort_is_not_an_error_for_any_provider(self):
        for provider in ("claude", "codex", "grok", "agy"):
            self.assertIsNone(
                effort_registry.validation_error(self.config, provider, None), provider
            )

    def test_config_key_comes_from_the_registry(self):
        self.assertEqual(effort_registry.config_key(self.config, "codex"), "model_reasoning_effort")


class DispatcherTests(unittest.TestCase):
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

    def environment(self):
        env = os.environ.copy()
        for name in ("CROSSCREW_SUPERVISED", "CROSSCREW_LAUNCHER_SIDECAR", "CROSSCREW_LAUNCHER_TOKEN"):
            env.pop(name, None)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["CROSSCREW_STATE_DIR"] = str(self.state)
        env["CROSSCREW_AGY_PROJECTS_DIR"] = str(self.base / "agy-projects")
        env["HOME"] = str(self.base)
        return env

    def invoke(self, provider, *extra, timeout=10):
        completed = subprocess.run(
            ["python3", str(CALL_WORKER), provider, str(self.brief), "--target", str(self.base), *extra],
            text=True,
            capture_output=True,
            env=self.environment(),
            timeout=timeout,
        )
        return completed, json.loads(completed.stdout)

    def test_codex_receives_the_effort_as_a_config_override(self):
        self.fake("codex", CODEX_ECHO)
        completed, payload = self.invoke("codex", "--effort", "max")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('-c model_reasoning_effort="max"', payload["stdout"])

    def test_envelope_reports_an_explicit_effort(self):
        self.fake("codex", CODEX_ECHO)
        _completed, payload = self.invoke("codex", "--effort", "max")
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertEqual(payload["reasoning_effort_source"], "explicit")

    def test_without_the_flag_no_override_is_sent_and_the_default_is_named(self):
        self.fake("codex", CODEX_ECHO)
        _completed, payload = self.invoke("codex")
        self.assertNotIn("model_reasoning_effort", payload["stdout"])
        self.assertIsNone(payload["reasoning_effort"])
        self.assertEqual(payload["reasoning_effort_source"], "provider_default_unpinned")

    def test_resume_keeps_the_override(self):
        self.fake("codex", CODEX_ECHO)
        _completed, payload = self.invoke(
            "codex", "--mode", "resume", "--session-id", "known-session", "--effort", "max"
        )
        self.assertIn('-c model_reasoning_effort="max"', payload["stdout"])

    def test_a_provider_that_declares_no_effort_is_rejected_before_spawning(self):
        self.fake("claude", "printf 'SHOULD_NOT_RUN\\n'")
        completed, payload = self.invoke("claude", "--effort", "max")
        self.assertEqual(completed.returncode, 64)
        self.assertEqual(payload["status"], "unsupported_effort")
        self.assertNotIn("SHOULD_NOT_RUN", json.dumps(payload))

    def test_an_undeclared_value_is_rejected_before_spawning(self):
        self.fake("codex", "printf 'SHOULD_NOT_RUN\\n'")
        completed, payload = self.invoke("codex", "--effort", "ultra")
        self.assertEqual(completed.returncode, 64)
        self.assertEqual(payload["status"], "unsupported_effort")
        self.assertEqual(payload["supported_efforts"], ["low", "medium", "high", "xhigh", "max"])

    def test_minimal_is_rejected_without_running_codex(self):
        self.fake("codex", 'touch "$HOME/provider-ran"')
        completed, payload = self.invoke("codex", "--effort", "minimal")
        self.assertEqual(completed.returncode, 64)
        self.assertEqual(payload["status"], "unsupported_effort")
        self.assertFalse((self.base / "provider-ran").exists())


class SupervisorTests(unittest.TestCase):
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
            ["python3", str(WORKER_JOB), *args],
            text=True,
            capture_output=True,
            env=self.environment(),
            timeout=15,
        )
        if expected is not None:
            self.assertEqual(completed.returncode, expected, completed.stderr)
        return completed, json.loads(completed.stdout)

    def wait_until_done(self, job_id):
        deadline = time.time() + 10
        while time.time() < deadline:
            _, status = self.invoke("status", job_id, expected=0)
            if status["status"] != "running":
                return status
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_start_propagates_the_effort_to_the_dispatcher(self):
        self.fake("codex", CODEX_ECHO)
        self.invoke(
            "start", "codex", str(self.brief),
            "--target", str(self.base),
            "--job-id", "job-effort",
            "--effort", "max",
            expected=0,
        )
        self.wait_until_done("job-effort")
        _completed, result = self.invoke("collect", "job-effort", expected=0)
        self.assertEqual(result["status"], "ok")
        self.assertIn('-c model_reasoning_effort="max"', result["stdout"])
        self.assertEqual(result["reasoning_effort"], "max")

    def test_job_metadata_records_the_effort(self):
        self.fake("codex", CODEX_ECHO)
        _completed, started = self.invoke(
            "start", "codex", str(self.brief),
            "--target", str(self.base),
            "--job-id", "job-effort-meta",
            "--effort", "max",
            expected=0,
        )
        self.assertEqual(started["reasoning_effort"], "max")

    def test_minimal_is_rejected_before_creating_a_job(self):
        self.fake("codex", 'touch "$HOME/provider-ran"')
        _, result = self.invoke(
            "start", "codex", str(self.brief), "--target", str(self.base),
            "--job-id", "rejected-minimal", "--effort", "minimal", expected=64,
        )
        self.assertEqual(result["status"], "unsupported_effort")
        self.assertFalse((self.state / "jobs" / "rejected-minimal").exists())
        self.assertFalse((self.base / "provider-ran").exists())


class BrokerContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.brief = self.base / "brief.md"
        self.brief.write_text("brief", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, **overrides):
        request = {
            "action": "start",
            "provider": "codex",
            "host": "claude",
            "profile": "review",
            "mode": "oneshot",
            "brief": str(self.brief),
            "target": str(self.base),
        }
        request.update(overrides)
        return request

    def test_declared_effort_is_accepted_and_normalized(self):
        request = protocol.validate(self.payload(effort="max"))
        self.assertEqual(request["effort"], "max")

    def test_undeclared_value_is_rejected(self):
        with self.assertRaises(protocol.RequestError) as caught:
            protocol.validate(self.payload(effort="ultra"))
        self.assertEqual(caught.exception.field, "effort")

    def test_minimal_is_rejected_at_the_broker_boundary(self):
        with self.assertRaises(protocol.RequestError) as caught:
            protocol.validate(self.payload(effort="minimal"))
        self.assertEqual(caught.exception.field, "effort")

    def test_provider_without_capability_is_rejected(self):
        with self.assertRaises(protocol.RequestError) as caught:
            protocol.validate(self.payload(provider="claude", effort="max"))
        self.assertEqual(caught.exception.field, "effort")

    def test_build_argv_forwards_the_effort(self):
        argv = protocol.build_argv(protocol.validate(self.payload(effort="max")))
        self.assertIn("--effort", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "max")

    def test_absent_effort_produces_no_flag(self):
        argv = protocol.build_argv(protocol.validate(self.payload()))
        self.assertNotIn("--effort", argv)


if __name__ == "__main__":
    unittest.main()
