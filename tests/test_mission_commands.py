"""호스트가 직접 쓰는 두 지점: mission 시작과 결과 판정.

판정은 사실 주장이므로 무결성을 검사한다. 존재하지 않는 job이나 다른 mission의
job을 accepted로 만들 수 있으면 원장이 거짓말을 한다.

무결성 검사를 테스트할 때 조심할 것이 하나 있다: **거부 상태 문자열만 보면
안 된다**. 잘못된 이유로 같은 문자열이 나올 수 있고, 거부하면서 이벤트를
남길 수도 있다. 그래서 여기서는 (1) 상태 문자열, (2) 종료 코드, (3) 거부 후
이벤트 로그가 그대로인지를 함께 본다.
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

import mission_log  # noqa: E402

WORKER_JOB = ROOT / "worker_job.py"


class MissionCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = self.base / "state"

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *args, expected=0):
        env = os.environ.copy()
        env["CROSSCREW_STATE_DIR"] = str(self.state)
        env["HOME"] = str(self.base)
        completed = subprocess.run(["python3", str(WORKER_JOB), *args],
                                   text=True, capture_output=True, env=env, timeout=20)
        self.assertEqual(completed.returncode, expected, completed.stderr)
        return json.loads(completed.stdout)

    def events(self, mission_id):
        return mission_log.read_events(self.state, mission_id)

    def kinds(self, mission_id, kind):
        return [event for event in self.events(mission_id) if event.get("e") == kind]

    def fake_job(self, mission_id, job_id, finished=True, worker_status="ok",
                 canceled=False, meta_extra=None):
        """mission에 속한 job 하나를 만든다.

        `started_epoch`는 반드시 '지금'이어야 한다. 0으로 두면 spawn grace가
        아득히 전에 만료된 것이 되어 projection이 이 job을 `failed`(= terminal)로
        읽는다. 그러면 "아직 끝나지 않은 job은 dispose할 수 없다"는 테스트가
        실제로는 terminal job을 검사하게 되어 아무것도 증명하지 못한다.
        """
        directory = self.state / "jobs" / job_id
        directory.mkdir(parents=True)
        meta = {"job_id": job_id, "provider": "codex", "mission_id": mission_id,
                "state": "running", "started_epoch": time.time(),
                "spawn_grace_seconds": 10, "pid": None}
        meta.update(meta_extra or {})
        (directory / "job.json").write_text(json.dumps(meta), encoding="utf-8")
        if finished:
            (directory / "result.pending.json").write_text(
                json.dumps({"status": worker_status}), encoding="utf-8")
        if canceled:
            (directory / "canceled.json").write_text(
                json.dumps({"status": "canceled"}), encoding="utf-8")
        return directory

    def unconfirmed_job(self, mission_id, job_id):
        """sidecar가 없어 liveness를 확정할 수 없는 job. terminal이 아니다."""
        return self.fake_job(mission_id, job_id, finished=False,
                             meta_extra={"pid": 999999})

    def open_mission(self, mission_id="m1"):
        return self.invoke("mission", "open", mission_id, "--goal", "g", "--target", str(self.base))

    # ------------------------------------------------------------------ open

    def test_open_records_goal_and_target(self):
        payload = self.invoke("mission", "open", "auth-refactor",
                              "--goal", "로그인 모듈 교체", "--target", str(self.base))
        self.assertEqual(payload["status"], "opened")
        event = self.events("auth-refactor")[0]
        self.assertEqual(event["e"], "mission.opened")
        self.assertEqual(event["goal"], "로그인 모듈 교체")
        self.assertEqual(event["target"], str(self.base.resolve()))

    def test_open_stamps_the_schema_version_and_a_timestamp(self):
        # 스펙 §3.4: 모든 이벤트는 v와 t를 갖는다. 호스트가 아니라 로그가 찍는다.
        self.open_mission()
        event = self.events("m1")[0]
        self.assertEqual(event["v"], mission_log.SCHEMA_VERSION)
        self.assertTrue(event["t"].endswith("Z"), event["t"])

    def test_open_refuses_a_target_that_is_not_a_directory(self):
        missing = self.base / "nope"
        payload = self.invoke("mission", "open", "m1", "--goal", "g",
                              "--target", str(missing), expected=66)
        self.assertEqual(payload["status"], "invalid_input")
        self.assertEqual(self.events("m1"), [])

    # -------------------------------------------------------------- dispose

    def test_dispose_records_the_verdict(self):
        self.open_mission()
        self.fake_job("m1", "job-1")
        payload = self.invoke("mission", "dispose", "m1", "job-1", "--rejected",
                              "--note", "테스트가 없다", "--changed-conclusion")
        self.assertEqual(payload["status"], "disposed")
        event = [e for e in self.events("m1") if e["e"] == "disposition.declared"][0]
        self.assertEqual(event["disposition"], "rejected")
        self.assertEqual(event["job_id"], "job-1")
        self.assertTrue(event["changed_host_conclusion"])
        self.assertEqual(event["note"], "테스트가 없다")

    def test_the_optional_disposition_fields_default_to_absent_not_true(self):
        # 위 테스트는 플래그를 준 경우만 본다. 상수 True를 쓰는 구현도 통과한다.
        self.open_mission()
        self.fake_job("m1", "job-1")
        self.invoke("mission", "dispose", "m1", "job-1", "--accepted")
        event = self.kinds("m1", "disposition.declared")[0]
        self.assertFalse(event["changed_host_conclusion"])
        self.assertIsNone(event["note"])

    def test_every_verdict_flag_is_recorded_under_its_own_name(self):
        self.open_mission()
        for flag, expected in (("--accepted", "accepted"), ("--rejected", "rejected"),
                               ("--repaired", "repaired"),
                               ("--closed-with-gaps", "closed_with_gaps")):
            job_id = f"job{flag}"
            self.fake_job("m1", job_id)
            payload = self.invoke("mission", "dispose", "m1", job_id, flag)
            self.assertEqual(payload["disposition"], expected, flag)
            event = [e for e in self.kinds("m1", "disposition.declared")
                     if e["job_id"] == job_id][0]
            self.assertEqual(event["disposition"], expected, flag)

    def test_dispose_requires_exactly_one_verdict(self):
        self.open_mission()
        self.fake_job("m1", "job-1")
        none_given = self.invoke("mission", "dispose", "m1", "job-1", expected=64)
        self.assertEqual(none_given["status"], "invalid_input")
        two_given = self.invoke("mission", "dispose", "m1", "job-1",
                                "--accepted", "--rejected", expected=64)
        self.assertEqual(two_given["status"], "invalid_input")
        self.assertEqual(self.kinds("m1", "disposition.declared"), [])

    def test_dispose_refuses_an_unopened_mission(self):
        self.fake_job("m1", "job-1")
        payload = self.invoke("mission", "dispose", "m1", "job-1", "--accepted", expected=64)
        self.assertEqual(payload["status"], "mission_not_open")

    def test_dispose_refuses_a_job_that_does_not_exist(self):
        self.open_mission()
        payload = self.invoke("mission", "dispose", "m1", "ghost", "--accepted", expected=64)
        self.assertEqual(payload["status"], "job_not_found")

    def test_a_job_id_that_looks_like_a_path_is_not_found_not_a_crash(self):
        self.open_mission()
        payload = self.invoke("mission", "dispose", "m1", "../../etc/passwd",
                              "--accepted", expected=64)
        self.assertEqual(payload["status"], "job_not_found")

    def test_dispose_refuses_a_job_from_another_mission(self):
        self.open_mission("m1")
        self.open_mission("m2")
        self.fake_job("m2", "job-2")
        payload = self.invoke("mission", "dispose", "m1", "job-2", "--accepted", expected=64)
        self.assertEqual(payload["status"], "job_mission_mismatch")
        self.assertEqual(self.kinds("m1", "disposition.declared"), [])
        self.assertEqual(self.kinds("m2", "disposition.declared"), [])

    def test_dispose_refuses_a_job_with_no_mission_label(self):
        # 라벨 없는 위임을 남의 mission 원장에 끼워 넣을 수 없다.
        self.open_mission("m1")
        self.fake_job(None, "job-plain")
        payload = self.invoke("mission", "dispose", "m1", "job-plain", "--accepted", expected=64)
        self.assertEqual(payload["status"], "job_mission_mismatch")

    def test_dispose_refuses_a_job_that_has_not_finished(self):
        self.open_mission()
        self.fake_job("m1", "job-live", finished=False)
        payload = self.invoke("mission", "dispose", "m1", "job-live", "--accepted", expected=64)
        self.assertEqual(payload["status"], "job_not_terminal")
        self.assertEqual(payload["lifecycle_status"], "starting")

    def test_dispose_refuses_a_job_whose_state_cannot_be_confirmed(self):
        # state_unconfirmed는 "끝났는지 모른다"는 뜻이다. 판정할 수 없다.
        self.open_mission()
        self.unconfirmed_job("m1", "job-unknown")
        payload = self.invoke("mission", "dispose", "m1", "job-unknown", "--accepted", expected=64)
        self.assertEqual(payload["status"], "job_not_terminal")
        self.assertEqual(payload["lifecycle_status"], "state_unconfirmed")

    def test_dispose_reads_live_state_not_the_state_field_in_job_json(self):
        # job.json의 state는 호출자가 쓴 값이다. 그것을 믿으면 아직 돌고 있는
        # job을 completed라고 적어 두는 것만으로 판정을 통과시킬 수 있다.
        self.open_mission()
        self.fake_job("m1", "job-liar", finished=False, meta_extra={"state": "completed"})
        payload = self.invoke("mission", "dispose", "m1", "job-liar", "--accepted", expected=64)
        self.assertEqual(payload["status"], "job_not_terminal")
        self.assertEqual(payload["lifecycle_status"], "starting")

    def test_a_failed_job_can_be_disposed(self):
        # terminal은 completed만이 아니다. 실패도 판정 대상이다.
        self.open_mission()
        self.fake_job("m1", "job-failed", worker_status="auth_expired")
        payload = self.invoke("mission", "dispose", "m1", "job-failed", "--rejected")
        self.assertEqual(payload["status"], "disposed")

    def test_a_canceled_job_can_be_disposed(self):
        self.open_mission()
        self.fake_job("m1", "job-canceled", finished=False, canceled=True)
        payload = self.invoke("mission", "dispose", "m1", "job-canceled", "--rejected")
        self.assertEqual(payload["status"], "disposed")

    def test_a_second_conflicting_disposition_is_refused(self):
        self.open_mission()
        self.fake_job("m1", "job-1")
        self.invoke("mission", "dispose", "m1", "job-1", "--accepted")
        payload = self.invoke("mission", "dispose", "m1", "job-1", "--rejected", expected=64)
        self.assertEqual(payload["status"], "already_disposed")
        self.assertEqual(len(self.kinds("m1", "disposition.declared")), 1)

    def test_even_an_identical_second_disposition_is_refused(self):
        # 판정은 append-only 로그의 사실 주장이다. 같은 값이라도 두 줄이 되면
        # 나중에 "몇 번 판정했는가"를 세는 쪽이 틀린 답을 얻는다.
        self.open_mission()
        self.fake_job("m1", "job-1")
        self.invoke("mission", "dispose", "m1", "job-1", "--accepted")
        payload = self.invoke("mission", "dispose", "m1", "job-1", "--accepted", expected=64)
        self.assertEqual(payload["status"], "already_disposed")

    def test_a_disposition_in_another_mission_does_not_block_this_one(self):
        # already_disposed 검사가 mission 경계를 넘어 job_id만 보면, 같은 이름의
        # job을 쓰는 다른 mission이 서로의 판정을 막는다.
        self.open_mission("m1")
        self.open_mission("m2")
        self.fake_job("m1", "shared-name")
        self.invoke("mission", "dispose", "m1", "shared-name", "--accepted")
        (self.state / "jobs" / "shared-name" / "job.json").write_text(
            json.dumps({"job_id": "shared-name", "provider": "codex", "mission_id": "m2",
                        "state": "running", "started_epoch": time.time(),
                        "spawn_grace_seconds": 10, "pid": None}), encoding="utf-8")
        payload = self.invoke("mission", "dispose", "m2", "shared-name", "--rejected")
        self.assertEqual(payload["status"], "disposed")

    def test_dispose_refuses_an_invalid_mission_id(self):
        payload = self.invoke("mission", "dispose", "a/b", "job-1", "--accepted", expected=64)
        self.assertEqual(payload["status"], "invalid_mission_id")

    # ---------------------------------------------------------------- close

    def test_clean_close_refuses_undisposed_jobs(self):
        # 미처분 job을 두고 깨끗하게 닫으면 완료로 위장된다.
        self.open_mission()
        self.fake_job("m1", "job-1")
        payload = self.invoke("mission", "close", "m1", expected=64)
        self.assertEqual(payload["status"], "undisposed_remaining")
        self.assertEqual(payload["undisposed"], ["job-1"])
        self.assertEqual(self.kinds("m1", "mission.closed"), [])

    def test_close_lists_only_the_jobs_that_are_actually_undisposed(self):
        self.open_mission()
        self.fake_job("m1", "job-done")
        self.fake_job("m1", "job-open")
        self.invoke("mission", "dispose", "m1", "job-done", "--accepted")
        payload = self.invoke("mission", "close", "m1", expected=64)
        self.assertEqual(payload["undisposed"], ["job-open"])

    def test_state_unconfirmed_also_blocks_a_clean_close(self):
        # 판정할 수 없는 job도 in-flight다. "결손 없음"을 주장하려면 모든 job의
        # 결말을 알아야 한다. 영원히 못 닫는 것을 막는 길은 --closed-with-gaps다.
        self.open_mission()
        self.unconfirmed_job("m1", "job-unknown")
        payload = self.invoke("mission", "close", "m1", expected=64)
        self.assertEqual(payload["status"], "jobs_in_flight")
        self.assertEqual(payload["in_flight"], ["job-unknown"])

    def test_close_ignores_undisposed_jobs_from_another_mission(self):
        self.open_mission("m1")
        self.open_mission("m2")
        self.fake_job("m2", "job-2")
        payload = self.invoke("mission", "close", "m1")
        self.assertEqual(payload["status"], "closed")

    def test_a_clean_close_records_no_gaps(self):
        self.open_mission()
        self.fake_job("m1", "job-1")
        self.invoke("mission", "dispose", "m1", "job-1", "--accepted")
        payload = self.invoke("mission", "close", "m1")
        self.assertEqual(payload["status"], "closed")
        event = self.kinds("m1", "mission.closed")[0]
        self.assertFalse(event["closed_with_gaps"])
        self.assertEqual(event["unresolved"], [])

    def test_clean_close_refuses_while_work_is_still_in_flight(self):
        # 실행 중인 job을 두고 깨끗하게 닫으면, 그 job이 끝난 뒤 미처분으로 남는데
        # mission은 이미 "결손 없음"을 주장하고 있다. 기록이 거짓말하게 된다.
        self.open_mission()
        self.fake_job("m1", "job-live", finished=False)
        payload = self.invoke("mission", "close", "m1", expected=64)
        self.assertEqual(payload["status"], "jobs_in_flight")
        self.assertEqual(payload["in_flight"], ["job-live"])
        self.assertEqual(self.events("m1")[-1]["e"], "mission.opened")   # 이벤트를 남기지 않았다

    def test_close_with_gaps_may_proceed_while_work_is_in_flight(self):
        # 멈춘 워커를 영원히 기다리게 만들면 안 된다. 결손으로 적고 닫는 길은 열어 둔다.
        self.open_mission()
        self.fake_job("m1", "job-live", finished=False)
        payload = self.invoke("mission", "close", "m1", "--closed-with-gaps",
                              "--unresolved", "job-live 미완료")
        self.assertEqual(payload["status"], "closed")

    def test_in_flight_check_does_not_fire_for_other_missions(self):
        self.open_mission("m1")
        self.open_mission("m2")
        self.fake_job("m2", "job-live", finished=False)
        self.assertEqual(self.invoke("mission", "close", "m1")["status"], "closed")

    def test_closed_with_gaps_requires_unresolved(self):
        self.open_mission()
        self.invoke("mission", "close", "m1", "--closed-with-gaps", expected=64)
        payload = self.invoke("mission", "close", "m1", "--closed-with-gaps",
                              "--unresolved", "성능 미검증")
        self.assertEqual(payload["status"], "closed")
        event = [e for e in self.events("m1") if e["e"] == "mission.closed"][0]
        self.assertEqual(event["unresolved"], ["성능 미검증"])

    def test_closed_with_gaps_without_unresolved_is_invalid_input_and_writes_nothing(self):
        self.open_mission()
        payload = self.invoke("mission", "close", "m1", "--closed-with-gaps", expected=64)
        self.assertEqual(payload["status"], "invalid_input")
        self.assertEqual(self.kinds("m1", "mission.closed"), [])

    def test_closed_with_gaps_carries_undisposed_jobs_past_the_gate(self):
        self.open_mission()
        self.fake_job("m1", "job-1")
        payload = self.invoke("mission", "close", "m1", "--closed-with-gaps",
                              "--unresolved", "job-1 판정 못 함")
        self.assertEqual(payload["status"], "closed")
        self.assertTrue(self.kinds("m1", "mission.closed")[0]["closed_with_gaps"])

    def test_close_refuses_an_unopened_mission(self):
        payload = self.invoke("mission", "close", "m1", expected=64)
        self.assertEqual(payload["status"], "mission_not_open")

    def test_close_refuses_an_invalid_mission_id(self):
        payload = self.invoke("mission", "close", "a/b", expected=64)
        self.assertEqual(payload["status"], "invalid_mission_id")

    # ------------------------------------------------------- mission id 검증

    def test_invalid_mission_id_is_rejected(self):
        self.invoke("mission", "open", "a/b", "--goal", "g", "--target", str(self.base), expected=64)

    def test_an_invalid_mission_id_creates_no_mission_directory(self):
        payload = self.invoke("mission", "open", "a/b", "--goal", "g",
                              "--target", str(self.base), expected=64)
        self.assertEqual(payload["status"], "invalid_mission_id")
        self.assertFalse((self.state / "missions").exists())

    # ------------------------------------- 관측 실패가 명령을 깨뜨리지 않는다

    def test_an_unwritable_log_degrades_open_instead_of_failing_it(self):
        # 스펙 §4.1. append 실패는 오류가 아니라 degraded 표시다.
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "missions").write_text("blocked", encoding="utf-8")
        payload = self.invoke("mission", "open", "m1", "--goal", "g", "--target", str(self.base))
        self.assertEqual(payload["status"], "opened")
        self.assertTrue(payload["event_log_degraded"])

    def test_a_successful_command_does_not_claim_to_be_degraded(self):
        payload = self.open_mission()
        self.assertFalse(payload["event_log_degraded"])

    def test_an_unwritable_log_degrades_dispose_instead_of_failing_it(self):
        self.open_mission()
        self.fake_job("m1", "job-1")
        (self.state / "missions" / "m1" / "events.jsonl").chmod(0o400)
        payload = self.invoke("mission", "dispose", "m1", "job-1", "--accepted")
        self.assertEqual(payload["status"], "disposed")
        self.assertTrue(payload["event_log_degraded"])
        # degraded가 진짜였는지 확인한다. 기록에 성공했다면 이 테스트는
        # 아무것도 증명하지 않는다.
        self.assertEqual(self.kinds("m1", "disposition.declared"), [])


if __name__ == "__main__":
    unittest.main()
