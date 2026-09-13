import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import job_wait
import projection

ROOT = Path(__file__).resolve().parents[1]


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.job = self.root / "jobs" / "sample"
        self.job.mkdir(parents=True)
        self.meta = {"state": "running", "pid": os.getpid(), "launcher_token": "fixture",
                     "sidecar_path": str(self.job / "launcher.json")}
        self.write("job.json", self.meta)
        self.write("launcher.json", {"launcher_token": "fixture"})

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        (self.job / name).write_text(json.dumps(value))

    def test_completed_ref_hashes_the_bytes_that_were_parsed_without_mutation(self):
        self.write("result.pending.json", {"status": "ok", "stdout": "PRIVATE_ANSWER"})
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in self.job.iterdir()}
        result = job_wait.snapshot(self.root, "sample")
        self.assertEqual(result["lifecycle_status"], "completed")
        raw = (self.job / "result.pending.json").read_bytes()
        self.assertEqual(result["result_ref"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["result_ref"]["size_bytes"], len(raw))
        self.assertNotIn("PRIVATE_ANSWER", str(result))
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                                  for p in self.job.iterdir()})
        self.assertFalse((self.job / "result.json").exists())

    def test_failed_envelope_is_returned_for_host_review(self):
        self.write("result.json", {"status": "auth_expired", "stdout": ""})
        row = job_wait.snapshot(self.root, "sample")
        self.assertEqual((row["lifecycle_status"], row["worker_status"]), ("failed", "auth_expired"))
        self.assertIsNotNone(row["result_ref"])

    def test_spawn_failure_and_cancellation_need_no_envelope(self):
        self.write("job.json", {"state": "spawn_failed"})
        row = job_wait.snapshot(self.root, "sample")
        self.assertEqual(row["lifecycle_status"], "failed")
        self.assertIsNone(row["result_ref"])
        self.write("canceled.json", {})
        self.assertEqual(job_wait.snapshot(self.root, "sample")["lifecycle_status"], "canceled")

    def test_unconfirmed_is_not_terminal_and_timeout_does_not_kill(self):
        (self.job / "launcher.json").unlink()
        with patch("os.kill", side_effect=AssertionError("must not signal")):
            rows = list(job_wait.observations(self.root, "sample", 0.05, 0.06))
        self.assertEqual(rows[-1]["wait_status"], "timeout")
        self.assertEqual(rows[-1]["lifecycle_status"], "state_unconfirmed")
        self.assertNotIn("state_unconfirmed", projection.TERMINAL)
        self.assertFalse((self.job / "canceled.json").exists())

    def test_partial_envelope_is_retried_until_complete(self):
        (self.job / "result.pending.json").write_text('{"status":')
        timer = threading.Timer(0.08, lambda: self.write("result.pending.json", {"status": "ok"}))
        timer.start()
        try:
            rows = list(job_wait.observations(self.root, "sample", 0.05, 2))
        finally:
            timer.join()
        self.assertEqual(rows[0]["wait_status"], "waiting")
        self.assertEqual(rows[-1]["wait_status"], "finished")

    def test_dead_worker_without_envelope_is_failed(self):
        with patch("projection._process_alive", return_value=False):
            row = job_wait.snapshot(self.root, "sample")
        self.assertEqual(row["lifecycle_status"], "failed")
        self.assertIsNone(row["result_ref"])

    def test_symlink_and_fifo_results_are_not_followed(self):
        outside = self.root / "outside.json"
        outside.write_text('{"status":"ok","stdout":"SECRET"}')
        path = self.job / "result.json"
        path.symlink_to(outside)
        self.assertIsNone(job_wait.snapshot(self.root, "sample")["result_ref"])
        path.unlink()
        os.mkfifo(path)
        row = job_wait.snapshot(self.root, "sample")
        self.assertIsNone(row["result_ref"])
        self.assertTrue(row["result_unreadable"])

    def test_wait_rejects_traversal_nonfinite_intervals_and_oversized_result(self):
        for name in ("../outside", ".", ".."):
            with self.assertRaises(ValueError):
                job_wait.snapshot(self.root, name)
        for interval, timeout in ((0, 0), (float("nan"), 0), (1, float("inf")), (1, -1)):
            with self.assertRaises(ValueError):
                list(job_wait.observations(self.root, "sample", interval, timeout))
        self.write("result.json", {"status": "ok", "stdout": "x" * 100})
        with patch("job_wait.MAX_RESULT_BYTES", 50):
            self.assertIsNone(job_wait.snapshot(self.root, "sample")["result_ref"])

    def test_waiter_termination_does_not_cancel_worker_and_reconnect_recovers(self):
        env = {**os.environ, "CROSSCREW_STATE_DIR": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"}
        command = [sys.executable, str(ROOT / "worker_job.py"), "wait", "sample", "--interval", "0.05"]
        ready = self.root / "waiter-ready"
        # Signal only after the real wait loop has entered its guarded snapshot,
        # rather than racing Python imports before KeyboardInterrupt is handled.
        bootstrap = (
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from pathlib import Path\nimport job_wait, worker_job\n"
            "original = job_wait.snapshot\n"
            "def observed_snapshot(*args, **kwargs):\n"
            "    row = original(*args, **kwargs)\n"
            f"    Path({str(ready)!r}).touch()\n"
            "    return row\n"
            "job_wait.snapshot = observed_snapshot\n"
            "worker_job.main()\n"
        )
        waiter = subprocess.Popen([sys.executable, "-c", bootstrap, *command[2:]],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and waiter.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "waiter did not enter the observation loop")
            waiter.send_signal(signal.SIGINT)
            stdout, _ = waiter.communicate(timeout=3)
        finally:
            if waiter.poll() is None:
                waiter.terminate()
                waiter.communicate(timeout=3)
        self.assertEqual(waiter.returncode, 130)
        self.assertEqual(json.loads(stdout)["wait_status"], "interrupted")
        self.assertFalse((self.job / "canceled.json").exists())
        self.write("result.pending.json", {"status": "ok", "stdout": "RECOVERED"})
        recovered = subprocess.run(command, env=env, capture_output=True, text=True, timeout=3)
        self.assertEqual(recovered.returncode, 0)
        self.assertIsNotNone(json.loads(recovered.stdout)["result_ref"])

    def test_watch_uses_the_same_readonly_path(self):
        self.write("result.pending.json", {"status": "error"})
        env = {**os.environ, "CROSSCREW_STATE_DIR": str(self.root)}
        r = subprocess.run([sys.executable, str(ROOT / "worker_job.py"), "watch", "sample"],
                           env=env, capture_output=True, text=True, timeout=3)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["worker_status"], "error")
        self.assertFalse((self.job / "result.json").exists())
