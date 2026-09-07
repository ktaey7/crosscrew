"""부작용 없는 lifecycle 투영.

세 가지를 보장한다.
(1) `.state`를 읽는 동안 아무것도 쓰지 않고 락도 잡지 않는다.
(2) allowlist에 없는 필드는 구조적으로 도달할 수 없다.
(3) 판정 불가를 running으로 추측하지 않는다.
"""

import fcntl
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

import projection  # noqa: E402

WORKER_JOB = ROOT / "worker_job.py"
SECRETS = ("launcher_token", "sidecar_path", "lock_path", "pid", "pgid", "session_id")


class JobFixture:
    """공통 픽스처. TestCase가 아니므로 수집되지 않는다 — 상속해도
    베이스의 테스트가 하위 클래스에서 다시 돌지 않는다."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        (self.state / "jobs").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def make_job(self, job_id, meta_extra=None, files=None):
        directory = self.state / "jobs" / job_id
        directory.mkdir()
        meta = {
            "schema_version": "1.0", "state": "running", "job_id": job_id,
            "provider": "codex", "host": "claude", "profile": "review",
            "mode": "oneshot", "target": "/abs/project", "run_id": "r1",
            "started_at": "2026-07-27T00:00:00Z", "started_epoch": time.time(),
            "spawn_grace_seconds": 10,
            "launcher_token": "SECRET-TOKEN",
            "sidecar_path": str(directory / "launcher.json"),
            "lock_path": str(directory / "launcher.lock"),
            "pid": None, "pgid": None, "session_id": "sess-abc",
        }
        meta.update(meta_extra or {})
        (directory / "job.json").write_text(json.dumps(meta), encoding="utf-8")
        (directory / "brief.md").write_text("SENSITIVE BRIEF BODY", encoding="utf-8")
        for name, payload in (files or {}).items():
            (directory / name).write_text(json.dumps(payload), encoding="utf-8")
        return directory

    def status_of(self, job_id):
        return {job["job_id"]: job for job in projection.snapshot(self.state)["jobs"]}[job_id]

    def dead_pid(self):
        """확실히 죽은 PID. subprocess.run은 CompletedProcess라 .pid가 없다."""
        process = subprocess.Popen(["true"])
        process.wait()
        return process.pid


class LifecycleTests(JobFixture, unittest.TestCase):
    """reducer가 각 canonical 파일 조합을 어떤 상태로 읽는가."""

    def test_canceled_wins_over_everything(self):
        self.make_job("j", files={"canceled.json": {"status": "canceled"},
                                  "result.pending.json": {"status": "ok"}})
        self.assertEqual(self.status_of("j")["lifecycle_status"], "canceled")

    def test_spawn_failed_is_failed(self):
        self.make_job("j", meta_extra={"state": "spawn_failed"})
        self.assertEqual(self.status_of("j")["lifecycle_status"], "failed")

    def test_pending_result_counts_as_finished(self):
        # result.json은 status 호출 전에는 없다. pending만 있어도 끝난 것이다.
        self.make_job("j", files={"result.pending.json": {"status": "ok", "duration_seconds": 3.5}})
        job = self.status_of("j")
        self.assertEqual(job["lifecycle_status"], "completed")
        self.assertEqual(job["worker_status"], "ok")
        self.assertEqual(job["duration_seconds"], 3.5)

    def test_pending_failure_is_failed_not_completed(self):
        self.make_job("j", files={"result.pending.json": {"status": "auth_expired"}})
        job = self.status_of("j")
        self.assertEqual(job["lifecycle_status"], "failed")
        self.assertEqual(job["worker_status"], "auth_expired")

    def test_no_pid_within_grace_is_starting(self):
        self.make_job("j", meta_extra={"pid": None, "started_epoch": time.time()})
        self.assertEqual(self.status_of("j")["lifecycle_status"], "starting")

    def test_no_pid_past_grace_is_failed(self):
        self.make_job("j", meta_extra={"pid": None, "started_epoch": time.time() - 600})
        self.assertEqual(self.status_of("j")["lifecycle_status"], "failed")

    def test_missing_sidecar_is_unconfirmed_not_running(self):
        self.make_job("j", meta_extra={"pid": 999999, "pgid": 999999})
        self.assertEqual(self.status_of("j")["lifecycle_status"], "state_unconfirmed")

    def test_live_process_with_matching_sidecar_is_running(self):
        directory = self.make_job("j", meta_extra={"pid": os.getpid(), "pgid": os.getpgrp()})
        (directory / "launcher.json").write_text(json.dumps({
            "launcher_token": "SECRET-TOKEN", "pid": os.getpid(), "pgid": os.getpgrp()}),
            encoding="utf-8")
        self.assertEqual(self.status_of("j")["lifecycle_status"], "running")

    def test_dead_process_without_result_is_failed(self):
        pid = self.dead_pid()
        directory = self.make_job("j", meta_extra={"pid": pid, "pgid": pid})
        (directory / "launcher.json").write_text(json.dumps({
            "launcher_token": "SECRET-TOKEN", "pid": pid, "pgid": pid}),
            encoding="utf-8")
        self.assertEqual(self.status_of("j")["lifecycle_status"], "failed")

    def test_sidecar_without_a_token_cannot_confirm_running(self):
        # 양쪽에 token이 없으면 None == None으로 "일치"해 버린다. 확인이 아니라 우연이다.
        directory = self.make_job("j", meta_extra={
            "pid": os.getpid(), "pgid": os.getpgrp(), "launcher_token": None})
        (directory / "launcher.json").write_text(json.dumps({
            "pid": os.getpid(), "pgid": os.getpgrp()}), encoding="utf-8")
        self.assertEqual(self.status_of("j")["lifecycle_status"], "state_unconfirmed")

    def test_every_status_is_a_declared_lifecycle_value(self):
        self.make_job("a", files={"canceled.json": {"status": "canceled"}})
        self.make_job("b", meta_extra={"state": "spawn_failed"})
        self.make_job("c", files={"result.pending.json": {"status": "ok"}})
        for job in projection.snapshot(self.state)["jobs"]:
            self.assertIn(job["lifecycle_status"], projection.LIFECYCLE, job["job_id"])


class DetailBoundTests(JobFixture, unittest.TestCase):
    def test_spawn_error_detail_is_truncated(self):
        # spawn_error는 임의의 str(exc)다. HTTP 표면으로 그대로 나가면 안 된다.
        self.make_job("j", meta_extra={"state": "spawn_failed", "spawn_error": "X" * 5000})
        detail = self.status_of("j")["detail"]
        self.assertLessEqual(len(detail), projection.DETAIL_MAX)
        self.assertTrue(detail.endswith("…"))

    def test_short_detail_is_left_alone(self):
        self.make_job("j", meta_extra={"state": "spawn_failed", "spawn_error": "no such file"})
        self.assertEqual(self.status_of("j")["detail"], "no such file")

    def test_schema_version_matches_the_repo_convention(self):
        # CLI envelope과 snapshot이 같은 데이터에 다른 타입을 붙이면 소비자가 갈린다.
        self.assertIsInstance(projection.SCHEMA_VERSION, str)
        self.assertEqual(projection.snapshot(self.state)["schema_version"],
                         projection.SCHEMA_VERSION)


class ExposureTests(JobFixture, unittest.TestCase):
    def test_secret_fields_never_appear(self):
        self.make_job("j")
        blob = json.dumps(projection.snapshot(self.state))
        for field in SECRETS:
            self.assertNotIn(field, blob, field)
        self.assertNotIn("SECRET-TOKEN", blob)

    def test_brief_body_never_appears(self):
        self.make_job("j")
        self.assertNotIn("SENSITIVE BRIEF BODY", json.dumps(projection.snapshot(self.state)))

    def test_result_stdout_never_appears(self):
        self.make_job("j", files={"result.pending.json": {
            "status": "ok", "stdout": "WORKER RAW OUTPUT", "duration_seconds": 12.5}})
        blob = json.dumps(projection.snapshot(self.state))
        self.assertNotIn("WORKER RAW OUTPUT", blob)
        self.assertIn("12.5", blob)

    def test_unknown_new_fields_are_not_leaked(self):
        self.make_job("j", meta_extra={"some_future_field": "LEAK-ME"})
        self.assertNotIn("LEAK-ME", json.dumps(projection.snapshot(self.state)))

    def test_expected_fields_carry_real_values(self):
        # key 존재만 보면 전부 None이어도 통과한다. 값을 검사한다.
        self.make_job("j")
        job = self.status_of("j")
        self.assertEqual(job["provider"], "codex")
        self.assertEqual(job["host"], "claude")
        self.assertEqual(job["profile"], "review")
        self.assertEqual(job["target"], "/abs/project")
        self.assertEqual(job["started_at"], "2026-07-27T00:00:00Z")


class NoSideEffectTests(JobFixture, unittest.TestCase):
    def snapshot_tree(self):
        return {
            str(path.relative_to(self.state)): (path.stat().st_mtime_ns, path.stat().st_size)
            for path in sorted(self.state.rglob("*")) if path.is_file()
        }

    def test_reading_writes_nothing(self):
        self.make_job("j", meta_extra={"pid": os.getpid(), "pgid": os.getpgrp()},
                      files={"result.pending.json": {"status": "ok"}})
        before = self.snapshot_tree()
        projection.snapshot(self.state)
        self.assertEqual(before, self.snapshot_tree())

    def test_no_result_json_is_materialized(self):
        directory = self.make_job("j", files={"result.pending.json": {"status": "ok"}})
        projection.snapshot(self.state)
        self.assertFalse((directory / "result.json").exists())

    def test_launcher_lock_is_never_created_or_taken(self):
        directory = self.make_job("j", meta_extra={"pid": os.getpid(), "pgid": os.getpgrp()})
        projection.snapshot(self.state)
        self.assertFalse((directory / "launcher.lock").exists())

    def test_no_flock_is_taken_even_when_the_lock_file_exists(self):
        # 위 테스트는 lock 파일이 없을 때만 본다. launcher_lock_held()는 없는 파일에
        # 락을 걸지 않으므로 그것만으로는 재사용을 잡지 못한다. flock 자체를 감시한다.
        directory = self.make_job("j", meta_extra={"pid": os.getpid(), "pgid": os.getpgrp()})
        (directory / "launcher.lock").write_text("", encoding="utf-8")
        (directory / "launcher.json").write_text(json.dumps({
            "launcher_token": "SECRET-TOKEN", "pid": os.getpid(), "pgid": os.getpgrp()}),
            encoding="utf-8")
        taken = []
        original = fcntl.flock
        fcntl.flock = lambda *args, **kwargs: taken.append(args)
        try:
            projection.snapshot(self.state)
        finally:
            fcntl.flock = original
        self.assertEqual(taken, [])

    def test_partial_write_is_skipped_not_fatal(self):
        self.make_job("good")
        broken = self.state / "jobs" / "broken"
        broken.mkdir()
        (broken / "job.json").write_text('{"job_id": "broken", ', encoding="utf-8")
        jobs = projection.snapshot(self.state)["jobs"]
        self.assertEqual([job["job_id"] for job in jobs], ["good"])

    def test_unusable_timing_fields_do_not_sink_the_whole_listing(self):
        # job.json이 파싱은 되는데 값이 이상한 경우. 한 job 때문에 목록 전체가
        # 죽으면 관측이 실행보다 약한 것이 아니라 아예 없는 것이 된다.
        self.make_job("good", meta_extra={"started_at": "2026-07-27T00:00:00Z"})
        self.make_job("odd", meta_extra={"started_at": "2026-07-26T00:00:00Z",
                                         "started_epoch": None,
                                         "spawn_grace_seconds": None, "pid": "not-a-pid"})
        jobs = projection.snapshot(self.state)["jobs"]
        self.assertEqual(sorted(job["job_id"] for job in jobs), ["good", "odd"])
        self.assertIn(self.status_of("odd")["lifecycle_status"], projection.LIFECYCLE)

    def test_jobs_are_newest_first(self):
        self.make_job("older", meta_extra={"started_at": "2026-07-26T00:00:00Z"})
        self.make_job("newer", meta_extra={"started_at": "2026-07-27T00:00:00Z"})
        self.assertEqual([job["job_id"] for job in projection.snapshot(self.state)["jobs"]],
                         ["newer", "older"])


class ListCommandTests(unittest.TestCase):
    """CLI 전체가 무쓰기여야 한다. projection 함수만 깨끗해서는 부족하다."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        directory = self.state / "jobs" / "j1"
        directory.mkdir(parents=True)
        (directory / "job.json").write_text(json.dumps({
            "job_id": "j1", "provider": "codex", "host": "claude", "profile": "review",
            "target": "/abs/project", "state": "running", "started_at": "2026-07-27T00:00:00Z",
            "started_epoch": time.time(), "spawn_grace_seconds": 10,
            "launcher_token": "SECRET-TOKEN", "pid": None,
        }), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def add_finished_job(self, job_id, started_at):
        directory = self.state / "jobs" / job_id
        directory.mkdir(parents=True)
        (directory / "job.json").write_text(json.dumps({
            "job_id": job_id, "provider": "grok", "host": "claude", "profile": "review",
            "target": "/abs/project", "state": "running", "started_at": started_at,
            "started_epoch": time.time(), "spawn_grace_seconds": 10,
            "launcher_token": "SECRET-TOKEN", "pid": None,
        }), encoding="utf-8")
        (directory / "result.pending.json").write_text(
            json.dumps({"status": "ok"}), encoding="utf-8")

    def tree(self):
        return {
            str(path.relative_to(self.state)): (path.stat().st_mode, path.stat().st_mtime_ns)
            for path in sorted(self.state.rglob("*"))
        }

    def run_list(self, *extra):
        env = os.environ.copy()
        env["CROSSCREW_STATE_DIR"] = str(self.state)
        env["HOME"] = str(self.base)
        completed = subprocess.run(["python3", str(WORKER_JOB), "list", *extra],
                                   text=True, capture_output=True, env=env, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_list_returns_the_projection(self):
        payload = self.run_list()
        self.assertEqual(payload["jobs"][0]["job_id"], "j1")
        self.assertEqual(payload["jobs"][0]["lifecycle_status"], "starting")

    def test_list_leaks_no_secrets(self):
        blob = json.dumps(self.run_list())
        self.assertNotIn("SECRET-TOKEN", blob)
        self.assertNotIn("launcher_token", blob)

    def test_list_does_not_touch_the_state_tree(self):
        # worker_job.state_dir() 은 모든 job 파일을 chmod 한다. list 는 그 경로를 쓰면 안 된다.
        before = self.tree()
        self.run_list()
        self.assertEqual(before, self.tree())

    def test_running_filter_and_limit_actually_filter(self):
        self.add_finished_job("j0", "2026-07-26T00:00:00Z")
        self.assertEqual([job["job_id"] for job in self.run_list()["jobs"]], ["j1", "j0"])
        self.assertEqual([job["job_id"] for job in self.run_list("--running")["jobs"]], ["j1"])
        self.assertEqual(len(self.run_list("--limit", "1")["jobs"]), 1)


if __name__ == "__main__":
    unittest.main()
