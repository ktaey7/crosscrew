"""mission 이벤트와 job 투영의 결합.

이게 없으면 화면은 실행 기록만 보여주고 goal·이견·처분은 영구히 비어 있다.

이 파일이 못 박는 것은 세 가지다.

1. **결합 자체** — 이벤트의 선언이 job과 mission 요약에 실제로 도달한다.
2. **결합의 의미론** — append-only 로그를 재생할 때 무엇이 이기는가. 계획서가
   정하지 않은 네 지점(재개봉·닫힌 뒤 판정·중복 판정·사라진 job)을 여기서 정한다.
   테스트가 결정의 기록이다.
3. **결합이 새는 경계** — 이벤트 본문은 호스트가 쓴 임의의 dict다. 스펙 §5.2가
   이름을 댄 필드만 밖으로 나간다. 특히 `changed_host_conclusion`은 스펙 §3.3이
   "지금 화면에 쓰지 않는다"고 명시한 필드이므로 도달 불가여야 한다.
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
import projection  # noqa: E402

WORKER_JOB = ROOT / "worker_job.py"


class StateFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        (self.state / "jobs").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def job(self, job_id, mission_id=None, worker_status="ok", profile="review",
            role=None, meta_extra=None):
        """job 하나를 만든다. worker_status=None이면 아직 안 끝난 job이다.

        `started_epoch`는 반드시 '지금'이다. 0으로 두면 spawn grace가 이미 만료된
        것이 되어 projection이 이 job을 failed(= terminal)로 읽고, "안 끝난 job은
        미처분으로 세지 않는다"는 테스트가 아무것도 증명하지 못한다.
        """
        directory = self.state / "jobs" / job_id
        directory.mkdir()
        meta = {
            "job_id": job_id, "provider": "codex", "host": "claude", "profile": profile,
            "target": "/abs/project", "state": "running", "role": role,
            "mission_id": mission_id, "started_at": "2026-07-27T00:00:00Z",
            "started_epoch": time.time(), "spawn_grace_seconds": 10, "pid": None,
        }
        meta.update(meta_extra or {})
        (directory / "job.json").write_text(json.dumps(meta), encoding="utf-8")
        if worker_status:
            (directory / "result.pending.json").write_text(
                json.dumps({"status": worker_status, "duration_seconds": 1.0}),
                encoding="utf-8")
        return directory

    def open_mission(self, mission_id="m1", goal="g", **extra):
        mission_log.append(self.state, mission_id, dict(
            {"e": "mission.opened", "mission_id": mission_id,
             "goal": goal, "target": "/abs/project"}, **extra))

    def dispose(self, mission_id, job_id, disposition, note=None, **extra):
        mission_log.append(self.state, mission_id, dict(
            {"e": "disposition.declared", "mission_id": mission_id, "job_id": job_id,
             "disposition": disposition, "note": note}, **extra))

    def close_mission(self, mission_id="m1", unresolved=(), gaps=False, **extra):
        mission_log.append(self.state, mission_id, dict(
            {"e": "mission.closed", "mission_id": mission_id,
             "closed_with_gaps": gaps, "unresolved": list(unresolved)}, **extra))

    def summary(self, mission_id, snapshot=None):
        snapshot = snapshot or projection.snapshot(self.state)
        for entry in snapshot["missions"]:
            if entry["mission_id"] == mission_id:
                return entry
        raise AssertionError(f"no summary for {mission_id}: {snapshot['missions']}")

    def job_of(self, job_id, snapshot=None):
        snapshot = snapshot or projection.snapshot(self.state)
        for entry in snapshot["jobs"]:
            if entry["job_id"] == job_id:
                return entry
        raise AssertionError(f"no job {job_id}")


class MissionJoinTests(StateFixture, unittest.TestCase):
    def test_goal_appears_on_the_mission_summary(self):
        self.open_mission("m1", goal="로그인 교체")
        self.job("j1", "m1")
        summary = projection.snapshot(self.state)["missions"][0]
        self.assertEqual(summary["mission_id"], "m1")
        self.assertEqual(summary["goal"], "로그인 교체")
        self.assertEqual(summary["target"], "/abs/project")
        self.assertEqual(summary["job_ids"], ["j1"])

    def test_disposition_lands_on_the_job(self):
        self.open_mission()
        self.dispose("m1", "j1", "rejected", note="근거 부족")
        self.job("j1", "m1")
        job = projection.snapshot(self.state)["jobs"][0]
        self.assertEqual(job["disposition"], "rejected")
        self.assertEqual(job["disposition_note"], "근거 부족")

    def test_a_finished_job_without_a_disposition_is_undisposed(self):
        self.open_mission()
        self.job("j1", "m1")
        snapshot = projection.snapshot(self.state)
        self.assertIsNone(snapshot["jobs"][0]["disposition"])
        self.assertEqual(snapshot["rail"]["undisposed"], 1)

    def test_a_running_job_is_not_counted_as_undisposed(self):
        self.job("j1", "m1", worker_status=None)
        snapshot = projection.snapshot(self.state)
        self.assertEqual(snapshot["jobs"][0]["lifecycle_status"], "starting")
        self.assertEqual(snapshot["rail"]["undisposed"], 0)

    def test_unlabeled_jobs_are_not_invented_into_a_mission(self):
        self.job("j1", None)
        snapshot = projection.snapshot(self.state)
        self.assertEqual(snapshot["missions"], [])
        self.assertIsNone(snapshot["jobs"][0]["mission_id"])

    def test_an_unlabeled_finished_job_is_counted_separately_not_hidden(self):
        # 레일은 열린 mission으로 범위를 좁힌다 — 그러지 않으면 라벨 이전의 옛 job이
        # 영구히 미처분 빚으로 잡혀 숫자를 못 쓴다. 그렇다고 조용히 지우면 라벨을
        # 안 붙이는 것으로 회계를 피할 수 있다. 따로 센다.
        self.job("j1", None)
        rail = projection.snapshot(self.state)["rail"]
        self.assertEqual(rail["undisposed"], 0)
        self.assertEqual(rail["unlabeled"], 1)

    def test_mission_events_without_jobs_still_produce_a_summary(self):
        self.open_mission("m1", goal="아직 위임 전")
        summary = projection.snapshot(self.state)["missions"][0]
        self.assertEqual(summary["goal"], "아직 위임 전")
        self.assertEqual(summary["job_ids"], [])

    def test_a_mission_directory_without_readable_events_is_still_listed(self):
        # append가 디렉터리를 만든 뒤 쓰기에 실패하면(event_log_degraded) 이 모양이
        # 된다. 조용히 숨기면 "mission이 없다"고 화면이 주장하게 된다. 비어 있음을
        # 비어 있는 채로 보인다.
        (self.state / "missions" / "ghost").mkdir(parents=True)
        summary = projection.snapshot(self.state)["missions"][0]
        self.assertEqual(summary["mission_id"], "ghost")
        self.assertIsNone(summary["goal"])
        self.assertFalse(summary["closed"])

    def test_jobs_of_two_missions_do_not_bleed_into_each_other(self):
        self.open_mission("m1")
        self.open_mission("m2")
        self.job("j1", "m1")
        self.job("j2", "m2")
        snapshot = projection.snapshot(self.state)
        self.assertEqual(self.summary("m1", snapshot)["job_ids"], ["j1"])
        self.assertEqual(self.summary("m2", snapshot)["job_ids"], ["j2"])


class RailTests(StateFixture, unittest.TestCase):
    """스펙 §7.1의 세 수치. 정의가 코드 한 곳에만 있어야 하므로 여기서만 검사한다."""

    def test_dissent_counts_verdicts_the_host_did_not_simply_accept(self):
        # 이견과 결손은 성격이 다르다. rejected/repaired는 "호스트가 워커 결과를
        # 그대로 받지 않았다", unresolved/closed_with_gaps는 "무엇을 못 했다"이다.
        # 한 칸에 섞으면 숫자를 보고도 무슨 일인지 알 수 없다 — 2026-07-27 실측에서
        # 진짜 반박 1건이 0으로, 잡기록 2줄이 이견으로 세어졌다.
        self.open_mission()
        self.job("j1", "m1")
        self.job("j2", "m1")
        self.dispose("m1", "j1", "closed_with_gaps")
        self.dispose("m1", "j2", "rejected")
        self.close_mission("m1", unresolved=["성능 미검증", "롤백 미확인"], gaps=True)
        snapshot = projection.snapshot(self.state)
        self.assertEqual(snapshot["rail"]["dissent"], 1)   # rejected 1
        self.assertEqual(snapshot["rail"]["gaps"], 3)      # closed_with_gaps 1 + unresolved 2
        self.assertEqual(self.summary("m1", snapshot)["unresolved"],
                         ["성능 미검증", "롤백 미확인"])
        self.assertTrue(self.summary("m1", snapshot)["closed"])
        self.assertTrue(self.summary("m1", snapshot)["closed_with_gaps"])

    def test_accepted_is_not_dissent_but_repaired_is(self):
        self.open_mission()
        self.job("j1", "m1")
        self.job("j2", "m1")
        self.dispose("m1", "j1", "accepted")
        self.dispose("m1", "j2", "repaired")
        self.close_mission("m1")
        snapshot = projection.snapshot(self.state)
        self.assertEqual(snapshot["rail"]["dissent"], 0)
        self.assertEqual(snapshot["rail"]["undisposed"], 0)

    def test_a_disposed_job_leaves_the_undisposed_count(self):
        self.open_mission()
        self.job("j1", "m1")
        self.job("j2", "m1")
        self.assertEqual(projection.snapshot(self.state)["rail"]["undisposed"], 2)
        self.dispose("m1", "j1", "accepted")
        self.assertEqual(projection.snapshot(self.state)["rail"]["undisposed"], 1)

    def test_a_canceled_job_is_terminal_and_therefore_disposable_debt(self):
        self.open_mission()
        directory = self.job("j1", "m1", worker_status=None)
        (directory / "canceled.json").write_text('{"status":"canceled"}', encoding="utf-8")
        snapshot = projection.snapshot(self.state)
        self.assertEqual(snapshot["jobs"][0]["lifecycle_status"], "canceled")
        self.assertEqual(snapshot["rail"]["undisposed"], 1)

    def test_terminal_is_a_subset_of_the_declared_lifecycle_values(self):
        # TERMINAL이 LIFECYCLE에 없는 문자열을 담으면 그 항목은 영원히 안 세진다.
        self.assertTrue(projection.TERMINAL <= set(projection.LIFECYCLE))
        self.assertNotIn("state_unconfirmed", projection.TERMINAL)

    def test_rail_counts_a_work_job_whose_window_is_unavailable(self):
        # changed_during_window는 아직 Plan B라 파이프라인이 만들지 않는다. 그래서
        # 이 테스트는 snapshot이 아니라 rail()을 직접 부른다. 산술은 검사하되
        # 파이프라인이 이 경로를 탄다고 주장하지 않는다.
        unavailable = {"lifecycle_status": "completed", "profile": "work",
                       "disposition": "accepted",
                       "changed_during_window": {"kind": "unavailable",
                                                 "reason": "not_a_git_repo"}}
        # 레일은 열린 mission에 속한 job만 센다. 산술만 보려면 mission을 함께 준다.
        open_mission = [{"mission_id": "m1", "closed": False, "unresolved": []}]
        unavailable = dict(unavailable, mission_id="m1")
        self.assertEqual(projection.rail([unavailable], open_mission)["unverified"], 1)
        observed = dict(unavailable, changed_during_window={"kind": "git", "changed": True})
        self.assertEqual(projection.rail([observed], open_mission)["unverified"], 0)
        self.assertEqual(
            projection.rail([dict(unavailable, profile="review")], open_mission)["unverified"], 0)
        # 닫힌 mission의 job은 레일에서 빠진다.
        closed = [{"mission_id": "m1", "closed": True, "unresolved": []}]
        self.assertEqual(projection.rail([unavailable], closed)["unverified"], 0)

    def test_snapshot_cannot_report_unverified_today(self):
        # 위 테스트가 "미검증 경로가 살아 있다"는 인상을 주지 않도록 짝으로 둔다.
        # 관측이 생기면 이 assertNotIn이 먼저 깨져서 여기를 다시 보게 된다.
        self.open_mission()
        self.job("j1", "m1", profile="work")
        snapshot = projection.snapshot(self.state)
        self.assertNotIn("changed_during_window", snapshot["jobs"][0])
        self.assertEqual(snapshot["rail"]["unverified"], 0)

    def test_the_rail_has_exactly_the_declared_numbers(self):
        rail = projection.snapshot(self.state)["rail"]
        self.assertEqual(sorted(rail),
                         ["dissent", "gaps", "undisposed", "unlabeled", "unverified"])
        for value in rail.values():
            self.assertIsInstance(value, int)


class ReplaySemanticsTests(StateFixture, unittest.TestCase):
    """append-only 로그를 재생할 때 무엇이 이기는가. 계획서가 정하지 않은 지점들."""

    def test_reopening_updates_the_goal_but_keeps_the_original_open_time(self):
        # 결정: 선언(goal·target)은 최신이 이긴다 — 호스트가 목표를 고쳐 쓸 수 있어야
        # 하고, append-only 로그의 재생은 관례적으로 최신 값을 현재 상태로 본다.
        # 반면 opened_at은 처음이 이긴다 — mission이 열린 시각은 사실이지 선언이
        # 아니다. 재개봉이 mission의 나이를 되돌리면 타임라인이 거짓말한다.
        self.open_mission("m1", goal="첫 목표")
        first = self.summary("m1")["opened_at"]
        time.sleep(1.1)      # 로그 타임스탬프는 초 해상도다
        self.open_mission("m1", goal="고친 목표")
        summary = self.summary("m1")
        self.assertEqual(summary["goal"], "고친 목표")
        self.assertEqual(summary["opened_at"], first)
        self.assertNotEqual(mission_log.read_events(self.state, "m1")[1]["t"], first)

    def test_a_disposition_after_the_mission_closed_still_lands(self):
        # 결정: 판정은 job에 매인 사실이지 mission 단계에 매인 것이 아니다. Task 6의
        # dispose는 닫힌 mission을 거부하지 않으므로(실제로 Task 6 스모크가 이 순서를
        # 만들었다) 여기서 무시하면 허용된 명령의 결과가 조용히 사라진다. 그러면 job은
        # 영원히 미처분으로 남아 레일이 갚을 수 없는 빚을 계속 센다.
        self.open_mission()
        self.job("j1", "m1")
        self.close_mission("m1", unresolved=["j1 미처분"], gaps=True)
        self.dispose("m1", "j1", "accepted", note="뒤늦게 확인")
        snapshot = projection.snapshot(self.state)
        self.assertEqual(snapshot["jobs"][0]["disposition"], "accepted")
        self.assertEqual(snapshot["jobs"][0]["disposition_note"], "뒤늦게 확인")
        self.assertEqual(snapshot["rail"]["undisposed"], 0)
        # 닫을 때 적어 둔 결손은 지워지지 않는다. 사후 판정이 과거 기록을 고치지 않는다.
        self.assertEqual(self.summary("m1", snapshot)["unresolved"], ["j1 미처분"])
        self.assertEqual(snapshot["rail"]["gaps"], 1)

    def test_the_last_disposition_wins_and_replaces_the_whole_declaration(self):
        # 결정: 중복 판정은 최신이 이긴다. Task 6은 두 번째를 거부하지만 로그는
        # append-only라 옛 버전이 남긴 두 줄이 있을 수 있다. 그때 나중 줄이 호스트의
        # 최신 의사다. 그리고 **선언 전체가 교체된다** — note를 병합하면 화면이
        # "rejected인데 사유는 승인 메모"라는 어디에도 없던 문장을 만들어 낸다.
        self.open_mission()
        self.job("j1", "m1")
        self.dispose("m1", "j1", "rejected", note="근거 부족")
        self.dispose("m1", "j1", "accepted")
        job = projection.snapshot(self.state)["jobs"][0]
        self.assertEqual(job["disposition"], "accepted")
        self.assertIsNone(job["disposition_note"])

    def test_a_disposition_for_a_job_that_is_gone_is_counted_not_dropped(self):
        # 결정: 조용히 사라지지 않는다. purge는 job 디렉터리를 지우지만 mission 로그는
        # 남는다. 그 판정을 그냥 버리면 요약은 "job 0건"이라고만 말하고, 실제로 있었던
        # 판정 3건은 어디에도 나타나지 않는다. 세어서 보인다.
        self.open_mission()
        self.dispose("m1", "purged-job", "accepted")
        summary = self.summary("m1")
        self.assertEqual(summary["job_ids"], [])
        self.assertEqual(summary["orphan_disposition_count"], 1)

    def test_a_disposition_cannot_reach_a_job_in_another_mission(self):
        # Task 6의 dispose는 job_mission_mismatch를 거부하지만 그것은 쓰기 경로다.
        # 읽기 경로가 전역 job 사전을 쓰면 손으로 고친(또는 옛 버전의) m1 로그 한 줄이
        # m2의 job을 accepted로 만들 수 있고, 미처분 레일이 조용히 줄어든다.
        self.open_mission("m1")
        self.open_mission("m2")
        self.job("j2", "m2")
        self.dispose("m1", "j2", "accepted")
        snapshot = projection.snapshot(self.state)
        self.assertIsNone(self.job_of("j2", snapshot)["disposition"])
        self.assertEqual(snapshot["rail"]["undisposed"], 1)
        self.assertEqual(self.summary("m1", snapshot)["orphan_disposition_count"], 1)
        self.assertEqual(self.summary("m2", snapshot)["orphan_disposition_count"], 0)

    def test_the_last_close_wins(self):
        self.open_mission()
        self.close_mission("m1", unresolved=["첫 결손"], gaps=True)
        self.close_mission("m1", unresolved=[], gaps=False)
        summary = self.summary("m1")
        self.assertTrue(summary["closed"])
        self.assertFalse(summary["closed_with_gaps"])
        self.assertEqual(summary["unresolved"], [])

    def test_an_unresolved_string_is_not_exploded_into_characters(self):
        # 옛 버전이나 손으로 쓴 줄이 unresolved를 리스트가 아니라 문자열로 남기면
        # 순진한 반복은 글자 수만큼 이견을 만든다. "성능 미검증" 한 줄이 이견 6이 된다.
        self.open_mission()
        mission_log.append(self.state, "m1", {"e": "mission.closed", "mission_id": "m1",
                                              "closed_with_gaps": True,
                                              "unresolved": "성능 미검증"})
        snapshot = projection.snapshot(self.state)
        self.assertEqual(self.summary("m1", snapshot)["unresolved"], [])
        self.assertEqual(snapshot["rail"]["dissent"], 0)

    def test_a_job_with_a_junk_mission_id_does_not_sink_the_listing(self):
        # job.json의 mission_id는 검증된 값이 아니다(옛 파일·손편집). 정렬 불가한 타입이
        # 섞이면 순진한 sorted()가 TypeError로 목록 전체를 죽인다. 관측이 실행보다
        # 시끄러워지면 안 된다(스펙 §4.1).
        self.open_mission("m1")
        self.job("good", "m1")
        self.job("numeric", 5)
        self.job("mapping", {"nope": 1})
        self.job("escape", "../../etc")
        snapshot = projection.snapshot(self.state)
        self.assertEqual(sorted(job["job_id"] for job in snapshot["jobs"]),
                         ["escape", "good", "mapping", "numeric"])
        self.assertEqual([entry["mission_id"] for entry in snapshot["missions"]], ["m1"])


class MissionExposureTests(StateFixture, unittest.TestCase):
    def test_no_event_body_leaks_beyond_the_allowlist(self):
        self.open_mission("m1", secret_extra="LEAK-OPEN")
        self.dispose("m1", "j1", "rejected", note="근거 부족", secret_extra="LEAK-DISPOSE")
        self.close_mission("m1", unresolved=["x"], gaps=True, secret_extra="LEAK-CLOSE")
        mission_log.append(self.state, "m1", {"e": "delegation.started", "mission_id": "m1",
                                              "job_id": "j1", "secret_extra": "LEAK-STARTED"})
        self.job("j1", "m1")
        blob = json.dumps(projection.snapshot(self.state))
        for marker in ("LEAK-OPEN", "LEAK-DISPOSE", "LEAK-CLOSE", "LEAK-STARTED",
                       "secret_extra"):
            self.assertNotIn(marker, blob, marker)

    def test_changed_host_conclusion_never_reaches_the_output(self):
        # 스펙 §3.3: 이 필드는 "지금 화면에 쓰지 않는다". 30건이 쌓인 뒤 답할 질문의
        # 재료지 표시물이 아니다. Task 6의 dispose는 이 값을 매번 쓰므로, allowlist가
        # 이름 단위로 막고 있다는 것을 값이 아니라 키로도 확인한다.
        self.open_mission()
        self.dispose("m1", "j1", "rejected", changed_host_conclusion=True)
        self.job("j1", "m1")
        snapshot = projection.snapshot(self.state)
        self.assertEqual(snapshot["jobs"][0]["disposition"], "rejected")
        self.assertNotIn("changed_host_conclusion", json.dumps(snapshot))

    def test_summary_keys_are_exactly_the_declared_allowlist(self):
        # 값만 보는 누출 테스트는 새 키가 추가돼도 통과한다. 키 집합을 못 박아야
        # 필드 추가가 allowlist 갱신을 강제한다.
        self.open_mission()
        self.job("j1", "m1")
        for summary in projection.snapshot(self.state)["missions"]:
            self.assertEqual(tuple(summary), projection.PUBLIC_MISSION_FIELDS)

    def test_the_snapshot_envelope_is_exactly_four_keys(self):
        self.assertEqual(sorted(projection.snapshot(self.state)),
                         ["jobs", "missions", "rail", "schema_version"])


class MissionNoSideEffectTests(StateFixture, unittest.TestCase):
    def tree(self):
        return {
            str(path.relative_to(self.state)): (path.stat().st_mode, path.stat().st_mtime_ns,
                                                path.stat().st_size)
            for path in sorted(self.state.rglob("*"))
        }

    def test_reading_mission_events_writes_nothing(self):
        self.open_mission()
        self.dispose("m1", "j1", "accepted")
        self.close_mission("m1", unresolved=["x"], gaps=True)
        self.job("j1", "m1")
        before = self.tree()
        projection.snapshot(self.state)
        self.assertEqual(before, self.tree())

    def test_the_missions_directory_is_never_created_by_reading(self):
        self.job("j1", "m1")
        projection.snapshot(self.state)
        self.assertFalse((self.state / "missions").exists())

    def test_an_unreadable_mission_log_is_skipped_not_fatal(self):
        self.open_mission("m1")
        (self.state / "missions" / "m1" / "events.jsonl").write_bytes(b"\xff\xfe not utf-8")
        self.open_mission("m2", goal="살아 있다")
        snapshot = projection.snapshot(self.state)
        self.assertEqual(self.summary("m1", snapshot)["goal"], None)
        self.assertEqual(self.summary("m2", snapshot)["goal"], "살아 있다")


class ListMissionCommandTests(StateFixture, unittest.TestCase):
    """CLI 표면. projection이 옳아도 배선이 틀리면 화면은 여전히 빈 값을 본다."""

    def run_list(self, *extra):
        env = os.environ.copy()
        env["CROSSCREW_STATE_DIR"] = str(self.state)
        env["HOME"] = str(self.base)
        completed = subprocess.run(["python3", str(WORKER_JOB), "list", *extra],
                                   text=True, capture_output=True, env=env, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def two_missions(self):
        self.open_mission("m1", goal="로그인 교체")
        self.open_mission("m2", goal="다른 건")
        self.job("j1", "m1")
        self.job("j2", "m2")
        self.job("j3", "m2")

    def test_list_carries_the_rail_and_the_mission_summaries(self):
        self.open_mission("m1", goal="로그인 교체")
        self.job("j1", "m1")
        payload = self.run_list()
        self.assertEqual(payload["rail"], {"dissent": 0, "undisposed": 1, "gaps": 0,
                                           "unverified": 0, "unlabeled": 0})
        self.assertEqual([entry["goal"] for entry in payload["missions"]], ["로그인 교체"])
        self.assertEqual(payload["jobs"][0]["disposition"], None)

    def test_the_mission_filter_scopes_jobs_missions_and_the_rail(self):
        # 한 mission을 보면서 전체 레일 숫자를 붙이면 화면이 남의 빚을 이 건의 빚으로
        # 보여 준다. 필터는 범위를 고르는 것이지 표시만 거르는 것이 아니다.
        self.two_missions()
        payload = self.run_list("--mission", "m2")
        self.assertEqual(sorted(job["job_id"] for job in payload["jobs"]), ["j2", "j3"])
        self.assertEqual([entry["mission_id"] for entry in payload["missions"]], ["m2"])
        self.assertEqual(payload["rail"]["undisposed"], 2)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(self.run_list()["rail"]["undisposed"], 3)

    def test_an_unknown_mission_is_distinguishable_from_an_empty_one(self):
        # 오타 난 id가 "빈 mission"과 같아 보이면 사용자는 없는 건을 비어 있다고 읽는다.
        self.open_mission("m1", goal="아직 위임 전")
        empty = self.run_list("--mission", "m1")
        self.assertEqual(empty["jobs"], [])
        self.assertEqual([entry["goal"] for entry in empty["missions"]], ["아직 위임 전"])
        typo = self.run_list("--mission", "m1-typo")
        self.assertEqual(typo["jobs"], [])
        self.assertEqual(typo["missions"], [])

    def test_the_running_filter_does_not_rewrite_the_rail(self):
        # --running은 표시할 부분집합이다. 여기서 레일을 다시 세면 undisposed가
        # 구조적으로 0이 되어 화면이 없는 안전을 주장한다.
        self.open_mission("m1")
        self.job("done", "m1")
        self.job("live", "m1", worker_status=None)
        payload = self.run_list("--running")
        self.assertEqual([job["job_id"] for job in payload["jobs"]], ["live"])
        self.assertEqual(payload["rail"]["undisposed"], 1)

    def test_the_limit_does_not_rewrite_the_rail(self):
        self.two_missions()
        payload = self.run_list("--limit", "1")
        self.assertEqual(len(payload["jobs"]), 1)
        self.assertEqual(payload["rail"]["undisposed"], 3)

    def test_the_disposition_reaches_the_cli(self):
        self.open_mission("m1")
        self.job("j1", "m1")
        self.dispose("m1", "j1", "rejected", note="근거 부족")
        payload = self.run_list("--mission", "m1")
        self.assertEqual(payload["jobs"][0]["disposition"], "rejected")
        self.assertEqual(payload["jobs"][0]["disposition_note"], "근거 부족")

    def test_list_with_missions_present_still_writes_nothing(self):
        # mission_log.append는 디렉터리를 0700으로 chmod한다. 읽기 경로가 그 코드를
        # 재사용하면 관람이 상태를 건드린다.
        self.open_mission("m1")
        self.job("j1", "m1")
        before = {str(path.relative_to(self.state)):
                  (path.stat().st_mode, path.stat().st_mtime_ns, path.stat().st_size)
                  for path in sorted(self.state.rglob("*"))}
        self.run_list("--mission", "m1")
        after = {str(path.relative_to(self.state)):
                 (path.stat().st_mode, path.stat().st_mtime_ns, path.stat().st_size)
                 for path in sorted(self.state.rglob("*"))}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
