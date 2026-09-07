import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import broker_endpoint  # noqa: E402
import broker_protocol as protocol  # noqa: E402

CALL_WORKER = ROOT / "call_worker.py"
WORKER_JOB = ROOT / "worker_job.py"
BROKER = ROOT / "multiai_broker.py"


class RequestValidationTests(unittest.TestCase):
    """The broker's request surface is its privilege boundary."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.brief = self.base / "brief.md"
        self.brief.write_text("task", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def valid(self, **overrides):
        payload = {
            "action": "start",
            "provider": "claude",
            "host": "codex",
            "profile": "review",
            "mode": "oneshot",
            "brief": str(self.brief),
            "target": str(self.base),
        }
        payload.update(overrides)
        return payload

    def test_accepts_a_well_formed_request(self):
        request = protocol.validate(self.valid())
        self.assertEqual(request["provider"], "claude")
        self.assertEqual(request["brief"], self.brief.resolve())

    def test_rejects_unknown_action(self):
        with self.assertRaises(protocol.RequestError) as ctx:
            protocol.validate(self.valid(action="exec"))
        self.assertEqual(ctx.exception.field, "action")

    def test_rejects_unknown_provider(self):
        with self.assertRaises(protocol.RequestError) as ctx:
            protocol.validate(self.valid(provider="bash"))
        self.assertEqual(ctx.exception.field, "provider")

    def test_rejects_unknown_host(self):
        with self.assertRaises(protocol.RequestError):
            protocol.validate(self.valid(host="root"))

    def test_rejects_relative_and_traversing_paths(self):
        for bad in ("relative/brief.md", "/tmp/../etc/passwd"):
            with self.assertRaises(protocol.RequestError):
                protocol.validate(self.valid(brief=bad))

    def test_rejects_missing_brief_and_target(self):
        with self.assertRaises(protocol.RequestError):
            protocol.validate(self.valid(brief=str(self.base / "absent.md")))
        with self.assertRaises(protocol.RequestError):
            protocol.validate(self.valid(target=str(self.base / "absent")))

    def test_rejects_identifier_metacharacters(self):
        for field in ("run_id", "session_id", "job_id"):
            with self.assertRaises(protocol.RequestError) as ctx:
                protocol.validate(self.valid(**{field: "a; rm -rf /"}))
            self.assertEqual(ctx.exception.field, field)

    def test_rejects_media_profile_for_non_grok(self):
        with self.assertRaises(protocol.RequestError):
            protocol.validate(
                self.valid(provider="claude", profile="media", mode="fresh", media_kind="image")
            )

    def test_rejects_media_options_without_media_profile(self):
        with self.assertRaises(protocol.RequestError) as ctx:
            protocol.validate(self.valid(media_kind="image"))
        self.assertEqual(ctx.exception.field, "profile")

    def test_rejects_out_of_range_numbers(self):
        with self.assertRaises(protocol.RequestError):
            protocol.validate(self.valid(check_after=10**9))
        with self.assertRaises(protocol.RequestError):
            protocol.validate(self.valid(check_after=-1))

    def test_build_argv_marks_the_call_as_escalated(self):
        argv = protocol.build_argv(protocol.validate(self.valid()))
        self.assertIn("--host-escalated", argv)
        self.assertIn(str(WORKER_JOB), argv)
        self.assertIn("start", argv)

    def test_build_argv_emits_only_validated_values(self):
        argv = protocol.build_argv(
            protocol.validate(self.valid(run_id="topic-1", session_id="abc.def-1"))
        )
        for token in argv:
            self.assertNotIn(";", token)
            self.assertNotIn("&", token)
            self.assertNotIn("|", token)

    def test_status_actions_need_only_a_job_id(self):
        request = protocol.validate({"action": "status", "job_id": "claude-1"})
        argv = protocol.build_argv(request)
        self.assertEqual(argv[-2:], ["status", "claude-1"])


class BrokerServerTests(unittest.TestCase):
    """End-to-end: a sandboxed host reaches a real broker over loopback."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.bin = cls.base / "bin"
        cls.bin.mkdir()
        cls.state = cls.base / "state"
        cls.brief = cls.base / "brief.md"
        cls.brief.write_text("Give an independent view.", encoding="utf-8")
        fake = cls.bin / "claude"
        fake.write_text("#!/bin/sh\nprintf 'BROKERED_OK\\n'\n", encoding="utf-8")
        fake.chmod(0o755)

        cls.env = os.environ.copy()
        for name in ("CROSSCREW_SUPERVISED", "CROSSCREW_LAUNCHER_SIDECAR", "CROSSCREW_LAUNCHER_TOKEN"):
            cls.env.pop(name, None)
        cls.env.pop("CROSSCREW_BROKER_DISABLE", None)
        cls.env["PATH"] = f"{cls.bin}:{cls.env['PATH']}"
        cls.env["CROSSCREW_STATE_DIR"] = str(cls.state)
        cls.env["CROSSCREW_AGY_PROJECTS_DIR"] = str(cls.base / "agy-projects")
        cls.env["HOME"] = str(cls.base)

        cls.log = open(cls.base / "broker.log", "w+", encoding="utf-8")
        cls.proc = subprocess.Popen(
            [sys.executable, str(BROKER), "--port", "0"],
            stdout=cls.log,
            stderr=cls.log,
            text=True,
            env=cls.env,
            cwd=str(ROOT),
        )
        deadline = time.time() + 15
        cls.info = None
        while time.time() < deadline:
            cls.info = broker_endpoint.read_endpoint(cls.state)
            if cls.info:
                break
            if cls.proc.poll() is not None:
                cls.log.seek(0)
                raise RuntimeError(f"broker exited: {cls.log.read()}")
            time.sleep(0.1)
        if not cls.info:
            cls.proc.kill()
            raise RuntimeError("broker did not register an endpoint")
        cls.token = broker_endpoint.read_token(cls.state)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.log.close()
        cls.temp.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.info['port']}{path}"

    def post(self, payload, token=None):
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Multiai-Token"] = token
        req = urllib.request.Request(self.url("/request"), data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_endpoint_is_loopback_only(self):
        self.assertEqual(self.info["host"], "127.0.0.1")

    def test_token_file_is_owner_only(self):
        mode = broker_endpoint.token_path(self.state).stat().st_mode
        self.assertEqual(mode & 0o077, 0)

    def test_health_needs_no_token(self):
        with urllib.request.urlopen(self.url("/health"), timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["status"], "ok")

    def test_missing_token_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post({"action": "check", "provider": "claude", "host": "codex"})
        self.assertEqual(ctx.exception.code, 401)
        ctx.exception.close()

    def test_wrong_token_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post({"action": "check", "provider": "claude", "host": "codex"}, token="nope")
        self.assertEqual(ctx.exception.code, 401)
        ctx.exception.close()

    def test_unknown_path_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.url("/exec"), timeout=10)
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()

    def test_invalid_request_is_rejected_before_any_spawn(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post({"action": "check", "provider": "bash", "host": "codex"}, token=self.token)
        self.assertEqual(ctx.exception.code, 400)
        payload = json.loads(ctx.exception.read().decode("utf-8"))
        ctx.exception.close()
        self.assertEqual(payload["status"], "broker_request_rejected")
        self.assertEqual(payload["field"], "provider")

    def test_check_through_broker_is_marked_as_brokered(self):
        status, payload = self.post(
            {"action": "check", "provider": "claude", "host": "codex"}, token=self.token
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["transport"], "broker")
        self.assertEqual(payload["escalated_via"], "broker")

    def test_broker_started_codex_job_exposes_progress_and_readonly_wait(self):
        import progress
        gate = self.base / 'codex-progress-gate'
        fake = self.bin / 'codex'
        fake.write_text(f'#!{sys.executable}\n' + '''import json,sys,time
from pathlib import Path
sys.stdin.read()
print(json.dumps({"type":"thread.started","thread_id":"01a079f5-f786-7c30-b9e7-f3ed9dbd13e9"}),flush=True)
gate=Path(''' + repr(str(gate)) + ''')
while not gate.exists(): time.sleep(.02)
Path(sys.argv[sys.argv.index('-o')+1]).write_text('BROKER_CODEX_OK')
print(json.dumps({"type":"turn.completed"}),flush=True)
''')
        fake.chmod(0o755)
        status, started = self.post({'action':'start', 'provider':'codex', 'host':'grok',
                                     'brief':str(self.brief), 'target':str(self.base),
                                     'profile':'review', 'mode':'fresh'}, token=self.token)
        self.assertEqual(status, 200)
        job_id = started['job_id']
        directory = self.state/'jobs'/job_id
        try:
            deadline = time.monotonic() + 5
            while not progress.read(directory) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertGreater(progress.read(directory).get('event_count', 0), 0)
        finally:
            gate.touch()
            ended = subprocess.run([sys.executable, str(ROOT/'worker_job.py'), 'wait', job_id,
                                    '--interval', '.05', '--timeout', '5'], env=self.env,
                                   capture_output=True, text=True, timeout=7)
        self.assertEqual(ended.returncode, 0, ended.stdout)
        result = json.loads(Path(json.loads(ended.stdout)['result_ref']['path']).read_text())
        self.assertEqual(result['host'], 'grok')
        self.assertEqual(result['stdout'], 'BROKER_CODEX_OK')
        self.assertFalse((directory/'result.json').exists())

    def test_job_cli_broker_start_and_resume_exit_successfully(self):
        session_ids = []
        for mode in ("fresh", "resume"):
            with self.subTest(mode=mode):
                started = subprocess.run(
                    [sys.executable, str(ROOT / "crosscrew.py"), "job", "start",
                     "claude", str(self.brief), "--host", "codex",
                     "--target", str(self.base), "--profile", "review",
                     "--mode", mode, "--run-id", "broker-cli-exit-regression"],
                    env=self.env, capture_output=True, text=True, timeout=30,
                )
                payload = json.loads(started.stdout)
                # Finish the fake worker even when the exit-code assertion fails.
                ended = subprocess.run(
                    [sys.executable, str(ROOT / "crosscrew.py"), "job", "wait",
                     payload["job_id"], "--timeout", "10", "--interval", ".05"],
                    env=self.env, capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(ended.returncode, 0, ended.stdout + ended.stderr)
                self.assertEqual(payload["status"], "running")
                self.assertEqual(payload["transport"], "broker")
                session_ids.append(payload["session_id"])
                self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual(session_ids[0], session_ids[1])

    def test_sandboxed_host_reaches_provider_through_broker_automatically(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(CALL_WORKER),
                "claude",
                str(self.brief),
                "--host",
                "codex",
                "--target",
                str(self.base),
            ],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=120,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "ok", payload)
        self.assertEqual(payload["transport"], "broker")
        self.assertEqual(payload["escalated_via"], "broker")
        self.assertIn("BROKERED_OK", payload["stdout"])


class BrokerAbsentTests(unittest.TestCase):
    """Without a broker the historical fail-closed behaviour must be unchanged."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        fake = self.bin / "claude"
        fake.write_text("#!/bin/sh\nprintf 'SHOULD_NOT_RUN\\n'\n", encoding="utf-8")
        fake.chmod(0o755)
        self.brief = self.base / "brief.md"
        self.brief.write_text("task", encoding="utf-8")
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin}:{self.env['PATH']}"
        self.env["CROSSCREW_STATE_DIR"] = str(self.base / "state")
        self.env["HOME"] = str(self.base)

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *extra):
        completed = subprocess.run(
            [
                sys.executable,
                str(CALL_WORKER),
                "claude",
                str(self.brief),
                "--target",
                str(self.base),
                *extra,
            ],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=60,
        )
        return completed, json.loads(completed.stdout)

    def test_broker_route_without_broker_still_fails_closed(self):
        completed, payload = self.invoke("--host", "codex")
        self.assertEqual(completed.returncode, 77)
        self.assertEqual(payload["status"], "needs_host_escalation")
        self.assertEqual(payload["route"], "broker")
        self.assertFalse(payload["broker_available"])
        self.assertNotIn("SHOULD_NOT_RUN", json.dumps(payload))

    def test_explicit_escalation_still_bypasses_the_broker(self):
        completed, payload = self.invoke("--host", "codex", "--host-escalated")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["transport"], "direct")

    def test_disable_flag_forces_the_fallback(self):
        self.env["CROSSCREW_BROKER_DISABLE"] = "1"
        completed, payload = self.invoke("--host", "codex")
        self.assertEqual(completed.returncode, 77)
        self.assertIn("disabled", payload["broker_error"])


if __name__ == "__main__":
    unittest.main()
