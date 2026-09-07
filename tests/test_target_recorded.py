"""target은 어느 프로젝트의 위임이었는지를 남기는 유일한 구조적 단서다.

지금은 argv로만 흐르고 job.json에도 result envelope에도 남지 않아, job 디렉터리
하나만 보고는 무슨 프로젝트 일이었는지 알 수 없다.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CALL_WORKER = ROOT / "call_worker.py"
WORKER_JOB = ROOT / "worker_job.py"


class TargetRecordedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.state = self.base / "state"
        self.project = self.base / "project"
        self.project.mkdir()
        self.brief = self.base / "brief.md"
        self.brief.write_text("brief", encoding="utf-8")
        fake = self.bin / "claude"
        fake.write_text("#!/bin/sh\nprintf 'OK\\n'\n", encoding="utf-8")
        fake.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def environment(self):
        env = os.environ.copy()
        for name in ("CROSSCREW_SUPERVISED", "CROSSCREW_LAUNCHER_SIDECAR", "CROSSCREW_LAUNCHER_TOKEN"):
            env.pop(name, None)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["CROSSCREW_STATE_DIR"] = str(self.state)
        env["HOME"] = str(self.base)
        return env

    def run_json(self, argv, timeout=20):
        completed = subprocess.run(argv, text=True, capture_output=True,
                                   env=self.environment(), timeout=timeout)
        return completed, json.loads(completed.stdout)

    def test_envelope_reports_the_target(self):
        _completed, payload = self.run_json(
            ["python3", str(CALL_WORKER), "claude", str(self.brief), "--target", str(self.project)])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["target"], str(self.project.resolve()))

    def test_job_metadata_persists_the_target(self):
        self.run_json(["python3", str(WORKER_JOB), "start", "claude", str(self.brief),
                       "--target", str(self.project), "--job-id", "job-target"])
        meta_path = self.state / "jobs" / "job-target" / "job.json"
        deadline = time.time() + 10
        while time.time() < deadline and not meta_path.is_file():
            time.sleep(0.05)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta["target"], str(self.project.resolve()))

    def test_start_response_reports_the_target(self):
        _completed, started = self.run_json(
            ["python3", str(WORKER_JOB), "start", "claude", str(self.brief),
             "--target", str(self.project), "--job-id", "job-target-resp"])
        self.assertEqual(started["target"], str(self.project.resolve()))


if __name__ == "__main__":
    unittest.main()
