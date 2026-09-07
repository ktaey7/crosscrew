#!/usr/bin/env python3
"""`.state`의 부작용 없는 lifecycle 투영.

`worker_job.status_payload()`는 읽기 함수가 아니다 — launcher를 복구하며
`job.json`을 다시 쓰고(`worker_job.py:226`), pending result를 `result.json`으로
materialize한다(`worker_job.py:303`). 관람 화면이 그것을 재사용하면 보는 행위가
상태를 바꾼다. 그래서 이 모듈이 따로 존재한다.

`worker_job.identity_status()`도 재사용하지 않는다. 그 함수는
`launcher_lock_held()`를 통해 launcher lock에 `LOCK_EX|LOCK_NB`를 건다
(`worker_job.py:150-159`). 여기서는 `os.kill(pid, 0)`만 쓰고, 그것으로 판정할 수
없으면 `running`으로 추측하지 않고 `state_unconfirmed`를 낸다.

노출 필드는 화이트리스트다. 블랙리스트로 두면 나중에 추가되는 필드가 기본
노출이 되는데 `job.json`에는 launcher token과 내부 경로가 들어 있다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

import mission_log

# 이 레포의 모든 envelope이 문자열 버전을 쓴다. CLI와 snapshot이 같은 데이터에
# 서로 다른 타입을 붙이면 소비자가 갈린다.
SCHEMA_VERSION = "1.0"

# detail은 spawn_error 같은 임의의 str(exc)를 담을 수 있다. HTTP 표면으로도
# 나가므로 길이를 묶는다. 진단 원문은 launcher.stderr에 있다.
DETAIL_MAX = 200

LIFECYCLE = ("starting", "running", "completed", "failed", "canceled", "state_unconfirmed")

# 더 이상 변하지 않는 상태. state_unconfirmed는 여기 없다 — 판정 불가는 종료가
# 아니다. worker_job이 이 집합을 다시 정의하지 않고 여기서 가져다 쓴다.
TERMINAL = {"completed", "failed", "canceled"}
_UNLOADED = object()

PUBLIC_JOB_FIELDS = (
    "job_id", "provider", "host", "profile", "mode", "target",
    "run_id", "started_at", "reasoning_effort",
    "mission_id", "role", "round",
)

# mission 요약이 밖으로 내보내는 전부. job과 마찬가지로 화이트리스트다 — 이벤트
# 본문은 호스트가 쓴 임의의 dict이고 `changed_host_conclusion`처럼 스펙이 아직
# 표시하지 말라고 한 필드도 그 안에 들어 있다.
PUBLIC_MISSION_FIELDS = ("mission_id", "goal", "target", "opened_at", "closed",
                         "closed_with_gaps", "unresolved", "job_ids",
                         "orphan_disposition_count")


def resolve_state_dir(config: dict, root: Path) -> Path:
    """worker_job.state_dir()과 달리 디렉터리를 만들지도 chmod하지도 않는다."""
    override = os.environ.get("CROSSCREW_STATE_DIR")
    if override:
        return Path(override).expanduser()
    path = Path(config.get("defaults", {}).get("state_dir", ".state")).expanduser()
    return path if path.is_absolute() else root / path


def _load(path: Path) -> dict | None:
    """읽고 파싱한다. 실패는 정상 상황이다 — job이 쓰는 중일 수 있다."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _bounded(text) -> str | None:
    """진단 문자열을 잘라 낸다. 임의 길이 str(exc)가 HTTP로 나가지 않게."""
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= DETAIL_MAX else text[: DETAIL_MAX - 1] + "…"


def _seconds(value, default: float) -> float:
    """job.json이 파싱은 되는데 값이 숫자가 아닐 수 있다. 목록 전체를 죽이지 않는다."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _process_alive(pid) -> bool | None:
    """True/False, 판정 불가면 None. 신호를 보내지 않는다(sig 0)."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 남의 프로세스지만 살아 있다
    except (OSError, TypeError, ValueError):
        return None
    return True


def lifecycle(directory: Path, meta: dict, *, result=_UNLOADED) -> tuple[str, str | None, str | None]:
    """(lifecycle_status, worker_status, detail). 아무것도 쓰지 않는다.

    판정 순서는 worker_job.status_payload()를 따른다. 다른 점은 launcher lock을
    잡지 않는다는 것뿐이고, 그래서 애매한 경우가 state_unconfirmed로 나온다.
    """
    if (directory / "canceled.json").is_file():
        return "canceled", None, "canceled by user"
    if meta.get("state") == "spawn_failed":
        return "failed", None, _bounded(meta.get("spawn_error") or "worker spawn failed")

    # collect(worker_job.py:983)와 같은 fallback. result.json은 status 호출 전에는 없다.
    if result is _UNLOADED:
        result = _load(directory / "result.json") or _load(directory / "result.pending.json")
    if result is not None:
        worker_status = result.get("status")
        return ("completed" if worker_status == "ok" else "failed"), worker_status, None

    pid = meta.get("pid")
    if pid is None:
        grace = _seconds(meta.get("spawn_grace_seconds"), 10.0)
        elapsed = max(0.0, time.time() - _seconds(meta.get("started_epoch"), 0.0))
        if elapsed <= grace:
            return "starting", None, None
        return "failed", None, "launcher did not register before spawn grace expired"

    token = meta.get("launcher_token")
    sidecar = _load(Path(meta["sidecar_path"])) if meta.get("sidecar_path") else None
    # token이 양쪽 다 없으면 None == None으로 통과한다. 그건 확인이 아니라 우연이다.
    if not token or not sidecar or sidecar.get("launcher_token") != token:
        return "state_unconfirmed", None, "launcher sidecar missing or mismatched"

    alive = _process_alive(pid)
    if alive is True:
        return "running", None, None
    if alive is False:
        return "failed", None, "worker exited without a result envelope"
    return "state_unconfirmed", None, "process liveness undeterminable"


def project_job(directory: Path, meta: dict) -> dict:
    job = {field: meta.get(field) for field in PUBLIC_JOB_FIELDS}
    status, worker_status, detail = lifecycle(directory, meta)
    job["lifecycle_status"] = status
    job["worker_status"] = worker_status
    job["detail"] = detail
    result = _load(directory / "result.json") or _load(directory / "result.pending.json")
    job["duration_seconds"] = (result or {}).get("duration_seconds")
    job["artifact_count"] = len((result or {}).get("artifacts") or [])
    # 결합 전 기본값. 판정이 없는 job은 "없음"이지 "미지"가 아니다 (스펙 §7.3.5).
    job["disposition"] = None
    job["disposition_note"] = None
    return job


def read_jobs(state_dir: Path) -> list[dict]:
    jobs_dir = state_dir / "jobs"
    if not jobs_dir.is_dir():
        return []
    jobs = []
    for directory in jobs_dir.iterdir():
        if not directory.is_dir():
            continue
        meta = _load(directory / "job.json")
        if not meta:
            continue
        jobs.append(project_job(directory, meta))
    jobs.sort(key=lambda job: job.get("started_at") or "", reverse=True)
    return jobs


def _mission_ids(state_dir: Path, jobs: list[dict]) -> list[str]:
    """job이 주장하는 mission + 디스크에 있는 mission. 둘 다 id를 검증한다.

    job.json의 mission_id는 검증된 값이 아니다 (옛 파일·손편집). 타입이 섞이면
    순진한 sorted()가 TypeError로 목록 전체를 죽인다.
    """
    ids = {job["mission_id"] for job in jobs
           if mission_log.valid_mission_id(job.get("mission_id"))}
    try:
        entries = list((state_dir / "missions").iterdir())
    except OSError:
        entries = []          # 없거나 못 읽는다. 관측은 조용히 비어 있으면 된다.
    for directory in entries:
        if mission_log.valid_mission_id(directory.name) and directory.is_dir():
            ids.add(directory.name)
    return sorted(ids)


def _unresolved(value) -> list[str]:
    """리스트가 아니면 버린다.

    문자열을 그대로 반복하면 "성능 미검증" 한 줄이 글자 수만큼의 이견이 된다.
    레일 숫자를 부풀리는 것은 침묵보다 나쁘다 — 없는 결손을 있다고 말한다.
    """
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def mission_summaries(state_dir: Path, jobs: list[dict]) -> list[dict]:
    """mission 이벤트를 읽어 요약을 만들고, **jobs를 제자리에서** 판정으로 채운다.

    재생 규칙 (append-only 로그라 순서가 의미를 갖는다):
    - `goal`·`target`은 최신 `mission.opened`가 이긴다. 선언은 고쳐 쓸 수 있다.
    - `opened_at`은 최초 `mission.opened`가 이긴다. 열린 시각은 선언이 아니라
      사실이고, 재개봉이 mission의 나이를 되돌리면 타임라인이 거짓말한다.
    - `disposition.declared`는 최신이 이기고 **선언 전체를 교체한다**. note만
      남겨 병합하면 "rejected인데 사유는 승인 메모"가 만들어진다.
    - 판정은 **그 mission에 속한 job에만** 닿는다. 전역 job 사전을 쓰면 손으로
      고친 m1 로그 한 줄이 m2의 job을 처분 완료로 만들 수 있다 (쓰기 경로의
      `job_mission_mismatch` 검사가 읽기 경로에는 없으므로).
    - 닿을 job이 없는 판정(purge됐거나 남의 것)은 버리지 않고 센다.

    이벤트 본문을 그대로 내보내지 않는다. PUBLIC_MISSION_FIELDS만 통과한다.
    """
    summaries = []
    for mission_id in _mission_ids(state_dir, jobs):
        mine = {job["job_id"]: job for job in jobs
                if job.get("mission_id") == mission_id}
        opened = closed = opened_at = None
        orphans = 0
        for event in mission_log.read_events(state_dir, mission_id):
            kind = event.get("e")
            if kind == "mission.opened":
                opened = event
                if opened_at is None:
                    opened_at = event.get("t")
            elif kind == "mission.closed":
                closed = event
            elif kind == "disposition.declared":
                job = mine.get(event.get("job_id"))
                if job is None:
                    orphans += 1
                    continue
                job["disposition"] = event.get("disposition")
                job["disposition_note"] = event.get("note")
        summaries.append(_public_mission({
            "mission_id": mission_id,
            "goal": (opened or {}).get("goal"),
            "target": (opened or {}).get("target"),
            "opened_at": opened_at,
            "closed": closed is not None,
            "closed_with_gaps": bool((closed or {}).get("closed_with_gaps")),
            "unresolved": _unresolved((closed or {}).get("unresolved")),
            "job_ids": sorted(mine),
            "orphan_disposition_count": orphans,
        }))
    return summaries


def _public_mission(record: dict) -> dict:
    """allowlist를 실제로 강제하는 지점. 여기를 지나지 않는 필드는 나가지 않는다."""
    return {field: record.get(field) for field in PUBLIC_MISSION_FIELDS}


def on_the_rail(mission: dict) -> bool:
    """이 mission이 아직 주의를 요구하는가.

    깨끗하게 닫힌 mission은 결산이 끝났으므로 상단에서 빠진다. 결손을 적고
    닫은 mission은 남는다 — 적어 둔 결손은 끝난 일이 아니다.
    """
    return not mission.get("closed") or bool(mission.get("unresolved"))


def rail(jobs: list[dict], missions: list[dict]) -> dict:
    """스펙 §7.1의 예외 레일. 정의를 코드 한 곳에만 둔다.

    네 수치를 나눠 세는 이유: `rejected`(호스트가 워커 결과를 안 받음)와
    `unresolved`(무엇을 못 했는지 적어 둠)는 성격이 다른데 한 칸에 섞으면
    "이견 2"를 보고도 무슨 일인지 알 수 없다. 2026-07-27 실측에서 실제로
    그랬다 — 진짜 반박 1건이 0으로 세어지고 잡기록 2줄이 이견으로 세어졌다.

    범위는 `on_the_rail`인 mission에 속한 job뿐이다. 전체를 세면 라벨 이전의
    옛 job 80건이 영구히 미처분 빚으로 잡혀 숫자가 못 쓰게 된다. 다만 라벨
    없는 job을 조용히 지우지는 않는다 — `unlabeled`로 따로 보인다.
    """
    active = {mission["mission_id"] for mission in missions if on_the_rail(mission)}
    # job.json의 mission_id는 검증된 값이 아니다. dict가 들어 있으면 `in set`이
    # TypeError로 목록 전체를 죽인다 — 관측이 실행보다 시끄러워지면 안 된다.
    scoped = [job for job in jobs
              if mission_log.valid_mission_id(job.get("mission_id"))
              and job["mission_id"] in active]
    finished = [job for job in scoped if job.get("lifecycle_status") in TERMINAL]
    return {
        # 호스트가 워커 결과를 그대로 받지 않은 것.
        "dissent": sum(1 for job in finished
                       if job.get("disposition") in ("rejected", "repaired")),
        # 끝났는데 아무도 판정하지 않은 것.
        "undisposed": sum(1 for job in finished if not job.get("disposition")),
        # 못 한 채로 남긴 것.
        "gaps": (
            sum(1 for job in finished if job.get("disposition") == "closed_with_gaps")
            + sum(len(mission.get("unresolved") or [])
                  for mission in missions if on_the_rail(mission))
        ),
        # changed_during_window는 아직 관측되지 않는다(Plan B). 오늘 이 값은
        # 구조적으로 0이다 — 계산이 있다는 것과 재료가 있다는 것은 다르다.
        "unverified": sum(
            1 for job in scoped
            if job.get("profile") == "work"
            and (job.get("changed_during_window") or {}).get("kind") == "unavailable"
        ),
        # mission에 속하지 않은 종료 job. 라벨을 안 붙이면 위 숫자에서 사라지므로
        # 회피가 눈에 보이게 따로 센다.
        "unlabeled": sum(1 for job in jobs
                         if not job.get("mission_id")
                         and job.get("lifecycle_status") in TERMINAL),
    }


def snapshot(state_dir: Path) -> dict:
    jobs = read_jobs(state_dir)
    missions = mission_summaries(state_dir, jobs)   # job에 disposition을 붙인다
    return {"schema_version": SCHEMA_VERSION, "jobs": jobs,
            "missions": missions, "rail": rail(jobs, missions)}
