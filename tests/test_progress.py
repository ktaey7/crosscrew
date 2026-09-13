import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import progress
import stream_process
import worker_job

ROOT = Path(__file__).resolve().parents[1]
SID = "01a079f5-f786-7c30-b9e7-f3ed9dbd13e9"


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.env = {**os.environ, "CROSSCREW_SUPERVISED": "1",
                    "CROSSCREW_PROGRESS_FILE": str(self.base / "progress.json"),
                    "CROSSCREW_LAUNCHER_SIDECAR": str(self.base / "launcher.json")}

    def tearDown(self):
        self.temp.cleanup()

    def test_only_allowlisted_metadata_is_persisted(self):
        writer = progress.Writer(self.env)
        for value in ({"type": "thread.started", "thread_id": SID, "prompt": "SECRET"},
                      {"type": "item.completed", "item": {"text": "SECRET"}},
                      {"type": "error", "message": "SECRET"}, {"type": "SECRET"}):
            writer.feed(json.dumps(value).encode() + b"\n")
        writer.close()
        raw = (self.base / "progress.json").read_bytes()
        self.assertNotIn(b"SECRET", raw)
        data = progress.read(self.base)
        self.assertEqual(data["event_count"], 3)
        self.assertEqual(data["session_id"], SID)
        self.assertEqual((self.base / "progress.json").stat().st_mode & 0o777, 0o600)
        self.assertLess(len(raw), progress.MAX_SNAPSHOT_BYTES)

    def test_fragmented_oversized_unknown_and_invalid_events_are_safe(self):
        writer = progress.Writer(self.env)
        writer.feed(b'{"type":"turn.st')
        writer.feed(b'arted"}\n')
        writer.feed(b'SECRET' * 20000 + b'\n')
        writer.feed(b'{"type":[]}\nnull\n[1]\n{bad SECRET}\n')
        writer.feed(b'{"type":"thread.started","thread_id":"SECRET"}\n')
        writer.feed(b'{"type":"turn.completed"}')
        writer.close()
        data = progress.read(self.base)
        self.assertEqual(data["event_count"], 3)
        self.assertIsNone(data["session_id"])
        self.assertTrue(data["observation_degraded"])
        self.assertNotIn("SECRET", (self.base / "progress.json").read_text())

    def test_symlink_or_external_path_never_receives_output(self):
        outside = self.base / "outside"
        outside.write_text("KEEP")
        (self.base / "progress.json").symlink_to(outside)
        writer = progress.Writer(self.env)
        writer.feed(b'{"type":"turn.started"}\n')
        writer.close()
        self.assertTrue(writer.degraded)
        self.assertEqual(outside.read_text(), "KEEP")
        other = progress.Writer({**self.env, "CROSSCREW_PROGRESS_FILE": str(outside)})
        self.assertTrue(other.degraded)
        other.close()

    def test_corrupt_or_partial_snapshot_is_not_a_signal(self):
        for text in ('{"event_count":', '{}', '[]'):
            (self.base / "progress.json").write_text(text)
            self.assertEqual(progress.read(self.base), {})

    def test_snapshot_write_failure_does_not_raise(self):
        writer = progress.Writer(self.env)
        with patch("progress.os.replace", side_effect=OSError("SECRET")):
            writer.feed(b'{"type":"thread.started","thread_id":"' + SID.encode() + b'"}\n')
        writer.close()
        self.assertTrue(writer.degraded)
        self.assertNotIn("SECRET", (self.base / "progress.json").read_text())
        self.assertFalse(list(self.base.glob('.progress-*')))

    def test_stdin_stdout_stderr_drain_without_deadlock_and_child_has_no_progress_path(self):
        writer = progress.Writer(self.env)
        code = ('import os,sys,json; '
                'sys.stderr.write("e"*200000);sys.stderr.flush(); '
                'sys.stdout.write(json.dumps({"type":"turn.started"})+"\\n");sys.stdout.flush(); '
                's=sys.stdin.read(); '
                'print(json.dumps({"type":"turn.completed","n":len(s),'
                '"path_leaked":"CROSSCREW_PROGRESS_FILE" in os.environ}))')
        rc, out, err, timed = stream_process.run([sys.executable, '-c', code], 'x'*200000,
                                                self.base, self.env, 5, writer, 4096, 1024)
        writer.close()
        self.assertEqual(rc, 0)
        self.assertFalse(timed)
        self.assertEqual(json.loads(out.splitlines()[-1])["n"], 200000)
        self.assertFalse(json.loads(out.splitlines()[-1])["path_leaked"])
        self.assertEqual(len(err), 1024)
        self.assertEqual(progress.read(self.base)["event_count"], 2)

    def test_explicit_timeout_keeps_partial_output(self):
        writer = progress.Writer(self.env)
        rc, out, _, timed = stream_process.run(
            [sys.executable, '-c', 'import time;print("partial",flush=True);time.sleep(5)'],
            None, self.base, self.env, 0.15, writer, 4096, 1024)
        writer.close()
        self.assertEqual(rc, 124)
        self.assertTrue(timed)
        self.assertIn("partial", out)

    def test_broken_observer_does_not_break_provider(self):
        writer = progress.Writer(self.env)
        with patch.object(writer, "feed", side_effect=RuntimeError("SECRET")):
            rc, out, _, _ = stream_process.run([sys.executable, '-c', 'print("OK")'], None,
                                               self.base, self.env, 2, writer, 4096, 1024)
        writer.close()
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "OK")
        self.assertTrue(writer.degraded)

    def test_supervised_fresh_and_oneshot_expose_progress_before_completion(self):
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "codex"
        fake.write_text(f'#!{sys.executable}\n' + '''import json,os,sys,time
from pathlib import Path
sys.stdin.read()
print(json.dumps({"type":"thread.started","thread_id":"''' + SID + '''"}),flush=True)
gate=Path(os.environ["TEST_PROGRESS_GATE"])
while not gate.exists(): time.sleep(.02)
Path(sys.argv[sys.argv.index("-o")+1]).write_text("FINAL_RESULT")
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":120,"cached_input_tokens":100,"output_tokens":20}}),flush=True)
''')
        fake.chmod(0o755)
        brief = self.base / "brief.md"
        brief.write_text("PRIVATE_BRIEF")
        for mode in ("fresh", "oneshot"):
            state = self.base / mode
            gate = self.base / (mode + '.gate')
            env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ['PATH'],
                   "CROSSCREW_STATE_DIR": str(state), "TEST_PROGRESS_GATE": str(gate),
                   "PYTHONDONTWRITEBYTECODE": "1"}
            for k in ("CROSSCREW_SUPERVISED", "CROSSCREW_LAUNCHER_SIDECAR", "CROSSCREW_LAUNCHER_TOKEN", "CROSSCREW_PROGRESS_FILE"):
                env.pop(k, None)
            started = subprocess.run([sys.executable, str(ROOT/'worker_job.py'), 'start', 'codex',
                                      str(brief), '--host', 'shell', '--target', str(self.base),
                                      '--mode', mode], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(started.returncode, 0, started.stderr)
            job_id = json.loads(started.stdout)['job_id']
            directory = state / 'jobs' / job_id
            try:
                deadline = time.monotonic() + 4
                while not progress.read(directory) and time.monotonic() < deadline:
                    time.sleep(.02)
                status = worker_job.status_payload(state, job_id)
                self.assertEqual(status['status'], 'running')
                self.assertEqual(status['session_id'], SID)
                self.assertEqual(status['attention_basis'], 'provider_event_stream')
                self.assertEqual(status['attention_signal'], 'strong')
                import job_wait
                before = (directory/'job.json').read_bytes()
                watched = job_wait.snapshot(state, job_id, worker_job.activity_payload)
                for key in ('attention_basis', 'attention_signal', 'session_id', 'event_count'):
                    self.assertEqual(watched[key], status[key])
                self.assertEqual((directory/'job.json').read_bytes(), before)
                self.assertFalse((directory/'result.pending.json').read_text())
            finally:
                gate.touch()
                ended = subprocess.run([sys.executable, str(ROOT/'worker_job.py'), 'wait', job_id,
                                        '--interval', '.05', '--timeout', '5'], env=env,
                                       capture_output=True, text=True, timeout=7)
            self.assertEqual(ended.returncode, 0, ended.stdout)
            result = json.loads(Path(json.loads(ended.stdout)['result_ref']['path']).read_text())
            self.assertEqual(result['stdout'], 'FINAL_RESULT')
            self.assertEqual(result['usage']['tokens']['input_tokens'], 120)
            self.assertEqual(result['usage']['source'], 'codex.turn.completed.usage')
            self.assertFalse(result['progress_degraded'])
            self.assertNotIn('usage', (directory/'progress.json').read_text())
            self.assertNotIn('PRIVATE_BRIEF', (directory/'progress.json').read_text())
