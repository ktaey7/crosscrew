"""돌고 있는 broker가 낡은 계약을 들고 있는 것을 감지한다.

broker는 launchd 상주 데몬이라 시작 시점의 `broker_protocol` 코드와
`backends.json` 레지스트리를 메모리에 스냅샷해 들고 있다. 계약을 바꾸고
재시작하지 않으면 새 필드를 `validate()`가 **조용히 버린다**.

2026-07-27 실측: 7/26 13:56에 뜬 broker가 7/27 14:59에 추가된 mission 라벨을
드롭했고, 오류도 경고도 없이 `status: running`을 반환했다. 단위 테스트는
디스크의 최신 코드를 import하고, 라이브 브로커 테스트는 브로커를 새로 띄우므로
둘 다 이 상태를 재현하지 못한다.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import broker_client  # noqa: E402
import broker_endpoint as endpoint  # noqa: E402
import broker_protocol as protocol  # noqa: E402


class ContractHashTests(unittest.TestCase):
    def test_hash_is_stable_across_calls(self):
        self.assertEqual(protocol.contract_hash(), protocol.contract_hash())

    def test_hash_is_a_short_hex_digest(self):
        value = protocol.contract_hash()
        self.assertRegex(value, r"^[0-9a-f]{16}$")

    def test_every_contract_file_exists(self):
        for name in protocol.CONTRACT_FILES:
            self.assertTrue((ROOT / name).is_file(), name)

    def test_the_spawned_scripts_are_not_contract_files(self):
        # broker는 worker_job/call_worker를 subprocess로 띄운다 — 항상 디스크의
        # 최신 코드다. 계약면에 넣으면 무관한 편집마다 오탐이 난다.
        for name in ("worker_job.py", "call_worker.py"):
            self.assertNotIn(name, protocol.CONTRACT_FILES)

    def test_changing_a_contract_file_changes_the_hash(self):
        target = ROOT / "broker_protocol.py"
        original = target.read_bytes()
        before = protocol.contract_hash()
        try:
            target.write_bytes(original + b"\n# staleness probe\n")
            self.assertNotEqual(protocol.contract_hash(), before)
        finally:
            target.write_bytes(original)
        self.assertEqual(protocol.contract_hash(), before)

    def test_changing_the_registry_changes_the_hash(self):
        # backends.json은 _REGISTRY_CACHE에 캐시되므로 두 번째 staleness 벡터다.
        self.assertIn("backends.json", protocol.CONTRACT_FILES)


class EndpointPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_endpoint_records_the_contract_hash(self):
        endpoint.write_endpoint(self.state, 5555, os.getpid(), "2026-07-27T00:00:00Z",
                                contract_hash="deadbeefdeadbeef")
        info = endpoint.read_endpoint(self.state)
        self.assertEqual(info["contract_hash"], "deadbeefdeadbeef")

    def test_an_endpoint_without_a_hash_is_refused(self):
        # 이 필드를 모르는 옛 broker가 광고한 endpoint다. 낡았다고 봐야 한다.
        path = endpoint.endpoint_path(self.state)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": "1.0", "host": "127.0.0.1",
                                    "port": 5555, "pid": 1, "started_at": "x"}),
                        encoding="utf-8")
        self.assertIsNone(endpoint.read_endpoint(self.state))


class ClientStalenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        endpoint.issue_token(self.state)

    def tearDown(self):
        self.temp.cleanup()

    def publish(self, contract_hash):
        endpoint.write_endpoint(self.state, 5555, os.getpid(), "2026-07-27T00:00:00Z",
                                contract_hash=contract_hash)

    def test_a_matching_hash_resolves_normally(self):
        self.publish(protocol.contract_hash())
        base, token = broker_client._resolve(self.state)
        self.assertTrue(base.startswith("http://127.0.0.1:"))
        self.assertTrue(token)

    def test_a_stale_broker_fails_closed(self):
        self.publish("0000000000000000")
        with self.assertRaises(broker_client.BrokerUnavailable) as caught:
            broker_client._resolve(self.state)
        self.assertIsInstance(caught.exception, broker_client.BrokerStale)

    def test_the_stale_message_says_how_to_fix_it(self):
        # "설치하라"가 아니라 "재시작하라"여야 한다. 원인이 다르다.
        self.publish("0000000000000000")
        with self.assertRaises(broker_client.BrokerStale) as caught:
            broker_client._resolve(self.state)
        self.assertIn("restart", str(caught.exception).lower())

    def test_stale_is_a_broker_unavailable_so_callers_still_fail_closed(self):
        # 기존 호출부는 BrokerUnavailable를 잡아 needs_host_escalation으로
        # 되돌린다. 새 예외를 그 계보 밖에 두면 조용히 터진다.
        self.assertTrue(issubclass(broker_client.BrokerStale,
                                   broker_client.BrokerUnavailable))

    def test_the_reason_is_recoverable_not_swallowed(self):
        # health()는 None만 돌려주므로 --check가 "broker_available: false"만 보이고
        # 이유가 사라진다. 사용자는 브로커가 죽은 줄 알고 health를 쳐서 정상이라는
        # 답을 받는다. 원인이 다른 두 상황을 구분할 수 있어야 한다.
        self.publish("0000000000000000")
        reason = broker_client.unavailable_reason(self.state)
        self.assertIsNotNone(reason)
        self.assertIn("restart", reason.lower())

    def test_a_healthy_contract_has_no_reason(self):
        self.publish(protocol.contract_hash())
        # 계약은 맞고 소켓만 없는 상태 — stale 사유는 아니다.
        reason = broker_client.unavailable_reason(self.state)
        self.assertTrue(reason is None or "restart" not in reason.lower())

    def test_check_surfaces_the_stale_reason(self):
        import subprocess
        self.publish("0000000000000000")
        env = os.environ.copy()
        env["CROSSCREW_STATE_DIR"] = str(self.state)
        env["HOME"] = str(self.state)
        for name in ("CROSSCREW_SUPERVISED", "CROSSCREW_LAUNCHER_SIDECAR",
                     "CROSSCREW_LAUNCHER_TOKEN", "CROSSCREW_BROKER_CHILD"):
            env.pop(name, None)
        completed = subprocess.run(
            ["python3", str(ROOT / "call_worker.py"), "claude",
             "--host", "codex", "--profile", "review", "--check"],
            text=True, capture_output=True, env=env, timeout=30)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["broker_available"])
        self.assertIn("restart", (payload.get("broker_unavailable_reason") or "").lower())

    def test_a_request_does_not_reach_the_socket_when_stale(self):
        # fail-closed의 요점: 낡은 broker에게는 보내지도 않는다.
        self.publish("0000000000000000")
        with self.assertRaises(broker_client.BrokerStale):
            broker_client.request(self.state, {"action": "check", "provider": "codex",
                                               "host": "claude"})


if __name__ == "__main__":
    unittest.main()
