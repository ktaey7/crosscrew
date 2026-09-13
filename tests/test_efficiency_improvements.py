import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

import call_worker
import grok_sandbox
import job_wait
import provider_usage
import usage_summary

ROOT = Path(__file__).resolve().parents[1]
SID = "11223344-5566-7788-99aa-bbccddeeff00"


class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"HOME": str(self.base), "GROK_HOME": str(self.base / ".grok"),
                                          "CROSSCREW_STATE_DIR": str(self.base / "state")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_gemini_all_codex_profiles_use_broker_without_writing_client_state(self):
        config = call_worker.load_config()
        brief = self.base / "brief.md"
        brief.write_text("Synthetic input. No dispatch is expected.")
        for profile in ("review", "research", "work"):
            self.assertEqual(call_worker.route_for(config, "codex", "agy", profile, "oneshot"), "broker")
            for script, args in (("call_worker.py", ["agy", str(brief)]),
                                 ("worker_job.py", ["start", "agy", str(brief)])):
                proc = subprocess.run([sys.executable, str(ROOT / script), *args, "--host", "codex",
                                       "--target", str(self.base), "--profile", profile],
                                      capture_output=True, text=True, timeout=10)
                result = json.loads(proc.stdout)
                self.assertEqual(result["status"], "needs_host_escalation", proc.stderr)
                self.assertFalse((self.base / "state").exists())

    def test_only_actual_seatbelt_startup_error_triggers_fallback(self):
        self.assertEqual(call_worker.classify(71, "", "sandbox-exec: sandbox_apply: Operation not permitted", False),
                         ("sandbox_conflict", True))
        self.assertEqual(call_worker.classify(1, "", "operation not permitted while reading input", False), ("blocked", False))
        self.assertEqual(call_worker.classify(0, "quoted example", "sandbox-exec: sandbox_apply: Operation not permitted", False), ("ok", False))

    def test_compact_wait_hashes_same_bytes_and_bounds_answer(self):
        job = self.base / "jobs" / "sample"
        job.mkdir(parents=True)
        (job / "job.json").write_text('{"state":"completed"}')
        value = {"status": "ok", "provider": "claude", "stdout": "한" * 6000,
                 "stderr_sanitized": "DO_NOT_RETURN", "usage": provider_usage.usage_record("claude", {"input_tokens": 8}, "claude.result.usage")}
        raw = json.dumps(value).encode()
        (job / "result.json").write_bytes(raw)
        row = job_wait.snapshot(self.base, "sample", summary=True)
        self.assertEqual(row["result_ref"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertLessEqual(len(row["result_summary"]["stdout"].encode()), 8192)
        self.assertTrue(row["result_summary"]["stdout_truncated"])
        self.assertNotIn("DO_NOT_RETURN", json.dumps(row))
        self.assertEqual(row["result_summary"]["usage"]["aggregation"], "invocation")
        (job / "result.json").unlink()
        (self.base / "secret.json").write_bytes(raw)
        (job / "result.json").symlink_to(self.base / "secret.json")
        self.assertNotIn("result_summary", job_wait.snapshot(self.base, "sample", summary=True))

    def test_native_grok_usage_requires_exact_fresh_session_and_regular_file(self):
        directory = self.base / ".grok" / "sessions" / quote(str(self.base.resolve()), safe="") / SID
        directory.mkdir(parents=True)
        source = directory / "usage.json"
        started = "2026-09-12T00:00:00+00:00"
        value = {"sessionId": SID, "updatedAt": "2026-09-12T00:00:01Z",
                 "session": {"inputTokens": 100, "outputTokens": 20, "cachedReadTokens": 80, "modelCalls": 2,
                             "secret": "DO_NOT_RETURN"}}
        source.write_text(json.dumps(value))
        usage = provider_usage.grok_usage(self.base, SID, started)
        self.assertEqual(usage["tokens"]["input_tokens"], 100)
        self.assertEqual(usage["aggregation"], "session_cumulative")
        self.assertNotIn("DO_NOT_RETURN", json.dumps(usage))
        self.assertIsNone(provider_usage.grok_usage(self.base, SID, "2026-09-12T00:00:02Z"))
        value["sessionId"] = "wrong"
        source.write_text(json.dumps(value))
        self.assertIsNone(provider_usage.grok_usage(self.base, SID, started))
        source.unlink()
        source.symlink_to(self.base / "anything")
        self.assertIsNone(provider_usage.grok_usage(self.base, SID, started))

    def write_counter(self, n, timestamp, **tokens):
        path = self.base / f"result-{n}.json"
        path.write_text(json.dumps({"schema_version": "2.0", "provider": "codex", "status": "ok", "session_id": SID,
                                   "started_at": timestamp, "run_id": f"job-{n}",
                                   "usage": provider_usage.usage_record("codex", tokens, "codex.turn.completed.usage")}))
        return path

    def test_resumes_and_duplicate_files_count_cumulative_usage_once(self):
        first = self.write_counter(1, "2026-09-12T00:00:00Z", input_tokens=100, output_tokens=10)
        second = self.write_counter(2, "2026-09-12T00:01:00Z", input_tokens=160, output_tokens=18)
        report = usage_summary.summarize([first, second, second])
        self.assertEqual(report["duplicate_files"], 1)
        group = report["providers"]["codex"]
        self.assertEqual(group["calls"], 2)
        self.assertEqual(group["tokens"]["input_tokens"]["reported_sum"], 160)
        self.assertEqual(group["tokens"]["output_tokens"]["reported_sum"], 18)
        self.assertEqual(group["sessions"], 1)

    def test_counter_reset_cannot_be_turned_into_a_total(self):
        first = self.write_counter(1, "2026-09-12T00:00:00Z", input_tokens=100, output_tokens=10)
        second = self.write_counter(2, "2026-09-12T00:01:00Z", input_tokens=60, output_tokens=8)
        group = usage_summary.summarize([second, first])["providers"]["codex"]
        self.assertEqual(group["unresolved_sessions"], 1)
        self.assertIsNone(group["tokens"]["input_tokens"]["reported_sum"])

    def test_grok_profile_install_is_additive_idempotent_and_refuses_widening(self):
        source = grok_sandbox.path()
        source.parent.mkdir()
        source.write_text('# keep this comment\n[profiles.existing]\nextends="workspace"\n')
        grok_sandbox.install()
        original = source.read_bytes()
        grok_sandbox.install()
        self.assertEqual(source.read_bytes(), original)
        self.assertTrue(grok_sandbox.ready())
        source.write_text(source.read_text().replace('extends = "strict"', 'extends = "workspace"'))
        self.assertFalse(grok_sandbox.ready())
        with self.assertRaises(ValueError):
            grok_sandbox.install()


if __name__ == "__main__":
    unittest.main()
