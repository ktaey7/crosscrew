"""mission 이벤트 로그와 라벨 전파.

이 로그는 관측물이지 실행 경로가 아니다. 쓰기가 실패해도 위임은 진행돼야 한다.
그리고 라벨은 broker hop을 통과해야 한다 — sandboxed host가 라벨이 가장
필요한 곳이다.
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

import broker_endpoint  # noqa: E402
import broker_protocol as protocol  # noqa: E402
import mission_log  # noqa: E402

WORKER_JOB = ROOT / "worker_job.py"
BROKER = ROOT / "multiai_broker.py"


def _options(argument_parser) -> set:
    return {flag for action in argument_parser._actions for flag in action.option_strings}


def _worker_job_start_options() -> set:
    import worker_job
    root = worker_job.parser()
    subs = next(a for a in root._actions if isinstance(getattr(a, "choices", None), dict))
    return _options(subs.choices["start"])


def _call_worker_options() -> set:
    import call_worker
    return _options(call_worker.parser())


class MissionLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        self.state.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_ids_accepted(self):
        for value in ("auth-refactor", "m1", "a.b_c-1"):
            self.assertTrue(mission_log.valid_mission_id(value), value)

    def test_ids_that_would_need_substitution_are_rejected(self):
        for value in ("a/b", "a b", "", "-leading", "x" * 65):
            self.assertFalse(mission_log.valid_mission_id(value), repr(value))

    def test_roles_are_bounded(self):
        self.assertTrue(mission_log.valid_role("구현"))
        self.assertFalse(mission_log.valid_role(""))
        self.assertFalse(mission_log.valid_role("x" * 33))
        self.assertFalse(mission_log.valid_role("two\nlines"))

    def test_append_then_read(self):
        self.assertTrue(mission_log.append(self.state, "m1", {"e": "mission.opened"}))
        self.assertTrue(mission_log.append(self.state, "m1", {"e": "delegation.started"}))
        self.assertEqual([e["e"] for e in mission_log.read_events(self.state, "m1")],
                         ["mission.opened", "delegation.started"])

    def test_caller_cannot_overwrite_version_or_timestamp(self):
        mission_log.append(self.state, "m1", {"e": "x", "v": 99, "t": "spoofed"})
        event = mission_log.read_events(self.state, "m1")[0]
        self.assertEqual(event["v"], 1)
        self.assertNotEqual(event["t"], "spoofed")
        self.assertTrue(event["t"].endswith("Z"))

    def test_rounds_are_bounded(self):
        self.assertTrue(mission_log.valid_round(1))
        self.assertTrue(mission_log.valid_round(mission_log.ROUND_MAX))
        for value in (0, -1, 100, "2", 1.0, True, None):
            self.assertFalse(mission_log.valid_round(value), repr(value))

    def test_log_file_is_private(self):
        mission_log.append(self.state, "m1", {"e": "mission.opened"})
        path = self.state / "missions" / "m1" / "events.jsonl"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_both_created_directories_are_private(self):
        # mkdir(parents=True)는 중간 디렉터리에 mode를 적용하지 않고 umask를 따른다.
        # mission 디렉터리만 0700이고 missions/가 0755면 제약을 반만 지킨 것이다.
        mission_log.append(self.state, "m1", {"e": "mission.opened"})
        self.assertEqual((self.state / "missions").stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state / "missions" / "m1").stat().st_mode & 0o777, 0o700)

    def test_invalid_id_returns_false_without_raising(self):
        self.assertFalse(mission_log.append(self.state, "a/b", {"e": "x"}))

    def test_unwritable_location_returns_false_without_raising(self):
        (self.state / "missions").write_text("not a directory", encoding="utf-8")
        self.assertFalse(mission_log.append(self.state, "m1", {"e": "x"}))

    def test_reading_a_missing_mission_is_empty(self):
        self.assertEqual(mission_log.read_events(self.state, "nope"), [])

    def test_a_corrupt_line_is_skipped_not_fatal(self):
        mission_log.append(self.state, "m1", {"e": "good"})
        path = self.state / "missions" / "m1" / "events.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"e": "broken"\n')
        self.assertEqual([e["e"] for e in mission_log.read_events(self.state, "m1")], ["good"])


class BrokerPropagationTests(unittest.TestCase):
    """라벨이 broker hop을 통과하지 못하면 sandboxed host에서 조용히 사라진다."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.brief = self.base / "brief.md"
        self.brief.write_text("brief", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, **overrides):
        request = {"action": "start", "provider": "codex", "host": "claude",
                   "profile": "review", "mode": "oneshot",
                   "brief": str(self.brief), "target": str(self.base)}
        request.update(overrides)
        return request

    def test_labels_survive_validation(self):
        request = protocol.validate(self.payload(mission="auth-refactor", role="구현", round=2))
        self.assertEqual(request["mission"], "auth-refactor")
        self.assertEqual(request["role"], "구현")
        self.assertEqual(request["round"], 2)

    def test_labels_reach_the_argv(self):
        argv = protocol.build_argv(protocol.validate(
            self.payload(mission="auth-refactor", role="구현", round=2)))
        self.assertEqual(argv[argv.index("--mission") + 1], "auth-refactor")
        self.assertEqual(argv[argv.index("--role") + 1], "구현")
        self.assertEqual(argv[argv.index("--round") + 1], "2")

    def test_absent_labels_produce_no_flags(self):
        argv = protocol.build_argv(protocol.validate(self.payload()))
        for flag in ("--mission", "--role", "--round"):
            self.assertNotIn(flag, argv)

    def test_invalid_mission_id_is_refused_at_the_boundary(self):
        with self.assertRaises(protocol.RequestError) as caught:
            protocol.validate(self.payload(mission="a/b"))
        self.assertEqual(caught.exception.field, "mission")

    def test_unbounded_role_is_refused(self):
        with self.assertRaises(protocol.RequestError) as caught:
            protocol.validate(self.payload(mission="m1", role="x" * 200))
        self.assertEqual(caught.exception.field, "role")

    def test_out_of_range_round_is_refused(self):
        for value in (0, -1, 1000, "two"):
            with self.assertRaises(protocol.RequestError):
                protocol.validate(self.payload(mission="m1", round=value))

    def test_mission_labels_are_refused_for_a_synchronous_call(self):
        # call_worker.py에는 --mission/--role/--round가 없다. 여기서 통과시키면
        # broker가 대상 CLI가 거부하는 argv를 만들거나 라벨을 조용히 버린다.
        for field in ("mission", "role", "round"):
            value = {"mission": "m1", "role": "구현", "round": 2}[field]
            with self.assertRaises(protocol.RequestError, msg=field) as caught:
                protocol.validate(self.payload(action="call", **{field: value}))
            self.assertEqual(caught.exception.field, "mission", field)

    def test_a_forged_call_request_still_builds_no_mission_flags(self):
        # validate를 통과한 뒤 request가 오염돼도 argv는 오염되지 않는다.
        request = protocol.validate(self.payload(action="call"))
        request.update({"mission": "auth-refactor", "role": "구현", "round": 2})
        argv = protocol.build_argv(request)
        for flag in ("--mission", "--role", "--round"):
            self.assertNotIn(flag, argv)

    def test_every_flag_the_broker_builds_is_declared_by_the_target_cli(self):
        # build_argv가 대상 CLI에 없는 플래그를 만들면 hop 전체가 죽는다.
        start = protocol.build_argv(protocol.validate(
            self.payload(mission="auth-refactor", role="구현", round=2)))
        self.assertLessEqual({flag for flag in start if flag.startswith("--")},
                             _worker_job_start_options())
        call = protocol.build_argv(protocol.validate(self.payload(action="call")))
        self.assertLessEqual({flag for flag in call if flag.startswith("--")},
                             _call_worker_options())

    def test_the_whole_supervisor_to_argv_hop_keeps_the_labels(self):
        # 아래 test_supervisor_forwards_labels_to_the_broker는 args를 위조하므로
        # 실제 argparse dest가 바뀌어도 통과한다. 여기서는 진짜 parser를 쓴다.
        import worker_job
        args = worker_job.parser().parse_args([
            "start", "claude", str(self.brief), "--host", "codex",
            "--target", str(self.base), "--mission", "auth-refactor",
            "--role", "구현", "--round", "2"])
        argv = protocol.build_argv(protocol.validate(
            worker_job.broker_start_payload(args, self.brief, self.base)))
        self.assertEqual(argv[argv.index("--mission") + 1], "auth-refactor")
        self.assertEqual(argv[argv.index("--role") + 1], "구현")
        self.assertEqual(argv[argv.index("--round") + 1], "2")

    def test_supervisor_forwards_labels_to_the_broker(self):
        import worker_job
        args = type("Args", (), {
            "provider": "claude", "host": "codex", "profile": "review", "mode": "oneshot",
            "run_id": "r1", "job_id": None, "session_id": None, "model": None, "effort": None,
            "media_kind": None, "output_dir": None, "aspect_ratio": None, "duration": None,
            "rehydrate_file": None, "check_after": None,
            "mission": "auth-refactor", "role": "구현", "round": 2,
        })()
        payload = worker_job.broker_start_payload(args, self.brief, self.base)
        self.assertEqual(payload["mission"], "auth-refactor")
        self.assertEqual(payload["role"], "구현")
        self.assertEqual(payload["round"], 2)


class SupervisorMissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.state = self.base / "state"
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

    def start(self, *extra):
        completed = subprocess.run(
            ["python3", str(WORKER_JOB), "start", "claude", str(self.brief),
             "--target", str(self.base), *extra],
            text=True, capture_output=True, env=self.environment(), timeout=20)
        return completed, json.loads(completed.stdout)

    def test_labels_land_in_metadata_and_the_event_log(self):
        _completed, started = self.start("--job-id", "j-mission", "--mission", "auth-refactor",
                                         "--role", "구현", "--round", "2")
        self.assertEqual(started["mission_id"], "auth-refactor")
        meta = json.loads((self.state / "jobs" / "j-mission" / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["role"], "구현")
        self.assertEqual(meta["round"], 2)
        events = [e for e in mission_log.read_events(self.state, "auth-refactor")
                  if e["e"] == "delegation.started"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["job_id"], "j-mission")
        self.assertEqual(events[0]["provider"], "claude")

    def test_an_invalid_mission_id_is_rejected_before_any_directory_is_created(self):
        completed, payload = self.start("--job-id", "j-bad", "--mission", "a/b")
        self.assertEqual(completed.returncode, 64)
        self.assertEqual(payload["status"], "invalid_mission_id")
        # orphan 디렉터리를 남기지 않는다
        self.assertFalse((self.state / "jobs" / "j-bad").exists())

    def test_an_invalid_role_is_rejected_before_any_directory_is_created(self):
        completed, payload = self.start("--job-id", "j-role", "--mission", "m1",
                                        "--role", "x" * 33)
        self.assertEqual(completed.returncode, 64)
        self.assertEqual(payload["status"], "invalid_role")
        self.assertFalse((self.state / "jobs" / "j-role").exists())
        # 거부된 위임은 이벤트도 남기지 않는다.
        self.assertEqual(mission_log.read_events(self.state, "m1"), [])

    def test_an_out_of_range_round_is_rejected_before_any_directory_is_created(self):
        for value in ("0", "100"):
            job_id = f"j-round-{value}"
            completed, payload = self.start("--job-id", job_id, "--mission", "m1",
                                            "--round", value)
            self.assertEqual(completed.returncode, 64, value)
            self.assertEqual(payload["status"], "invalid_round", value)
            self.assertFalse((self.state / "jobs" / job_id).exists(), value)

    def test_a_job_without_a_mission_still_runs(self):
        completed, started = self.start("--job-id", "j-plain")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(started["status"], "running")
        self.assertEqual(started.get("exit_code"), 0)
        self.assertIsNone(started["mission_id"])

    def test_event_log_failure_does_not_fail_the_job(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "missions").write_text("blocked", encoding="utf-8")
        _completed, started = self.start("--job-id", "j-degraded",
                                         "--mission", "auth-refactor", "--role", "구현")
        self.assertEqual(started["status"], "running")
        self.assertTrue(started["event_log_degraded"])
        # degraded가 진짜였는지 확인한다. 플래그만 세워 두고 실제로는 기록에
        # 성공했다면 이 테스트는 아무것도 증명하지 않는다.
        self.assertEqual(mission_log.read_events(self.state, "auth-refactor"), [])
        # 그리고 위임 자체는 끝까지 살아 있어야 한다. 파일이 비어 있지 않은 것만
        # 보면 워커가 실패 envelope을 뱉었어도 통과하므로 내용을 검사한다.
        pending = self.state / "jobs" / "j-degraded" / "result.pending.json"
        envelope = None
        deadline = time.time() + 20
        while time.time() < deadline and envelope is None:
            try:
                envelope = json.loads(pending.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.05)
        self.assertIsNotNone(envelope, "worker never wrote a result envelope")
        self.assertEqual(envelope["status"], "ok", envelope)
        self.assertIn("OK", envelope["stdout"])


class BrokeredMissionTests(unittest.TestCase):
    """Codex host -> Claude는 `broker` route다.

    validate와 build_argv를 따로 검사하는 것으로는 hop이 실제로 이어졌음을
    증명하지 못한다. 진짜 broker를 띄우고, 반대편에서 시작된 job의 job.json과
    이벤트 로그에 라벨이 있는지 본다.
    """

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.bin = cls.base / "bin"
        cls.bin.mkdir()
        cls.state = cls.base / "state"
        cls.brief = cls.base / "brief.md"
        cls.brief.write_text("brief", encoding="utf-8")
        fake = cls.bin / "claude"
        fake.write_text("#!/bin/sh\nprintf 'BROKERED_OK\\n'\n", encoding="utf-8")
        fake.chmod(0o755)

        cls.env = os.environ.copy()
        for name in ("CROSSCREW_SUPERVISED", "CROSSCREW_LAUNCHER_SIDECAR", "CROSSCREW_LAUNCHER_TOKEN",
                     "CROSSCREW_BROKER_CHILD"):
            cls.env.pop(name, None)
        cls.env.pop("CROSSCREW_BROKER_DISABLE", None)
        cls.env["PATH"] = f"{cls.bin}:{cls.env['PATH']}"
        cls.env["CROSSCREW_STATE_DIR"] = str(cls.state)
        cls.env["CROSSCREW_AGY_PROJECTS_DIR"] = str(cls.base / "agy-projects")
        cls.env["HOME"] = str(cls.base)

        cls.log = open(cls.base / "broker.log", "w+", encoding="utf-8")
        cls.proc = subprocess.Popen([sys.executable, str(BROKER), "--port", "0"],
                                    stdout=cls.log, stderr=cls.log, text=True,
                                    env=cls.env, cwd=str(ROOT))
        deadline = time.time() + 15
        while time.time() < deadline and not broker_endpoint.read_endpoint(cls.state):
            if cls.proc.poll() is not None:
                cls.log.seek(0)
                raise RuntimeError(f"broker exited: {cls.log.read()}")
            time.sleep(0.1)
        if not broker_endpoint.read_endpoint(cls.state):
            cls.proc.kill()
            raise RuntimeError("broker did not register an endpoint")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.log.close()
        cls.temp.cleanup()

    def start(self, *extra):
        completed = subprocess.run(
            [sys.executable, str(WORKER_JOB), "start", "claude", str(self.brief),
             "--host", "codex", "--target", str(self.base), *extra],
            text=True, capture_output=True, env=self.env, timeout=120)
        return completed, json.loads(completed.stdout)

    def broker_log_lines(self):
        path = broker_endpoint.request_log_path(self.state)
        try:
            return path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def test_labels_reach_the_job_started_on_the_other_side_of_the_broker(self):
        completed, payload = self.start("--job-id", "j-brokered", "--mission", "auth-refactor",
                                         "--role", "구현", "--round", "2")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(payload["status"], "running", payload)
        self.assertEqual(payload.get("exit_code"), 0)
        self.assertEqual(payload["transport"], "broker", payload)
        self.assertEqual(payload["mission_id"], "auth-refactor", payload)
        meta = json.loads(
            (self.state / "jobs" / "j-brokered" / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["mission_id"], "auth-refactor")
        self.assertEqual(meta["role"], "구현")
        self.assertEqual(meta["round"], 2)
        events = [e for e in mission_log.read_events(self.state, "auth-refactor")
                  if e["e"] == "delegation.started"]
        self.assertEqual([e["job_id"] for e in events], ["j-brokered"])
        self.assertEqual(events[0]["role"], "구현")
        self.assertEqual(events[0]["round"], 2)

    def test_brokered_fresh_start_exit_matches_running_acceptance(self):
        # Observed live: JSON status=running with job_id/session_id, shell exit 1.
        completed, payload = self.start(
            "--job-id", "j-accepted-running", "--mode", "fresh", "--profile", "review"
        )
        self.assertEqual(payload["status"], "running", payload)
        self.assertEqual(payload["job_id"], "j-accepted-running")
        self.assertTrue(payload.get("session_id"), payload)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(payload.get("exit_code"), 0)
        self.assertEqual(payload["transport"], "broker", payload)

    def test_an_invalid_label_never_reaches_the_broker(self):
        before = len(self.broker_log_lines())
        completed, payload = self.start("--job-id", "j-brokered-bad", "--mission", "a/b")
        self.assertEqual(completed.returncode, 64)
        self.assertEqual(payload["status"], "invalid_mission_id")
        self.assertEqual(len(self.broker_log_lines()), before)
        self.assertFalse((self.state / "jobs" / "j-brokered-bad").exists())


if __name__ == "__main__":
    unittest.main()
