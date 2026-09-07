"""run registry 파일명 충돌과 소유권.

run ID는 파일명으로 치환되므로 서로 다른 두 run이 같은 파일로 수렴할 수 있다.
그때 session ID를 조용히 공유하면 서로 다른 작업이 같은 provider 대화에
올라탄다. 소유권은 write lock 안에서 확정한다.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_registry  # noqa: E402

CALL_WORKER = ROOT / "call_worker.py"


class RunRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        (self.state / "runs").mkdir(parents=True)
        (self.state / "locks").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, filename, payload):
        path = self.state / "runs" / filename
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_two_run_ids_collapse_to_the_same_path(self):
        self.assertEqual(
            run_registry.registry_path(self.state, "a/b"),
            run_registry.registry_path(self.state, "a b"),
        )

    def test_missing_file_yields_an_empty_registry(self):
        registry = run_registry.read(self.state, "fresh-run")
        self.assertEqual(registry["run_id"], "fresh-run")
        self.assertEqual(registry["providers"], {})

    def test_matching_run_id_reads_normally(self):
        self.write("a_b.json", {"schema_version": "1.0", "run_id": "a/b",
                                "providers": {"claude": {"session_id": "s1"}}})
        registry = run_registry.read(self.state, "a/b")
        self.assertEqual(registry["providers"]["claude"]["session_id"], "s1")

    def test_foreign_run_id_in_the_same_file_is_rejected(self):
        self.write("a_b.json", {"schema_version": "1.0", "run_id": "a/b", "providers": {}})
        with self.assertRaises(run_registry.RunIdCollision) as caught:
            run_registry.read(self.state, "a b")
        self.assertEqual(caught.exception.requested, "a b")
        self.assertEqual(caught.exception.stored, "a/b")

    def test_claim_creates_the_file_and_records_the_owner(self):
        run_registry.claim(self.state, "brand-new")
        stored = json.loads((self.state / "runs" / "brand-new.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["run_id"], "brand-new")

    def test_claim_is_idempotent_for_the_same_owner(self):
        run_registry.claim(self.state, "same")
        run_registry.claim(self.state, "same")   # raise 하지 않는다

    def test_claim_refuses_a_file_owned_by_another_run(self):
        run_registry.claim(self.state, "a/b")
        with self.assertRaises(run_registry.RunIdCollision):
            run_registry.claim(self.state, "a b")

    def test_claim_does_not_erase_existing_sessions(self):
        self.write("keep.json", {"schema_version": "1.0", "run_id": "keep",
                                 "providers": {"claude": {"session_id": "s9"}}})
        run_registry.claim(self.state, "keep")
        registry = run_registry.read(self.state, "keep")
        self.assertEqual(registry["providers"]["claude"]["session_id"], "s9")

    def test_session_lookup_refuses_a_foreign_session(self):
        self.write("a_b.json", {"schema_version": "1.0", "run_id": "a/b",
                                "providers": {"claude": {"session_id": "s1"}}})
        with self.assertRaises(run_registry.RunIdCollision):
            run_registry.session_for(self.state, "a b", "claude")

    def test_legacy_file_without_run_id_is_adopted_not_rejected(self):
        # 이전 스키마 파일이 있어도 읽기를 막지 않는다. claim이 소유자를 적어 준다.
        self.write("legacy.json", {"schema_version": "1.0", "providers": {}})
        self.assertEqual(run_registry.read(self.state, "legacy")["providers"], {})
        run_registry.claim(self.state, "legacy")
        stored = json.loads((self.state / "runs" / "legacy.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["run_id"], "legacy")

    def test_corrupt_registry_fails_closed(self):
        (self.state / "runs" / "bad.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(run_registry.RunIdCollision):
            run_registry.claim(self.state, "bad")

    def test_claimed_file_is_private(self):
        run_registry.claim(self.state, "private-run")
        path = self.state / "runs" / "private-run.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class DispatcherCollisionTests(unittest.TestCase):
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

    def invoke(self, *extra):
        env = os.environ.copy()
        for name in ("CROSSCREW_SUPERVISED", "CROSSCREW_LAUNCHER_SIDECAR", "CROSSCREW_LAUNCHER_TOKEN"):
            env.pop(name, None)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["CROSSCREW_STATE_DIR"] = str(self.state)
        env["HOME"] = str(self.base)
        completed = subprocess.run(
            ["python3", str(CALL_WORKER), "claude", str(self.brief),
             "--target", str(self.base), *extra],
            text=True, capture_output=True, env=env, timeout=20)
        return completed, json.loads(completed.stdout)

    def test_colliding_run_id_is_refused_before_spawning(self):
        _completed, first = self.invoke("--mode", "fresh", "--run-id", "a/b")
        self.assertEqual(first["status"], "ok")
        completed, second = self.invoke("--mode", "resume", "--run-id", "a b")
        self.assertEqual(completed.returncode, 64)
        self.assertEqual(second["status"], "run_id_collision")
        self.assertEqual(second["conflicting_run_id"], "a/b")

    def test_a_corrupt_registry_tells_the_user_how_to_get_unstuck(self):
        # 손상은 충돌과 조치가 다르다. "run ID를 바꿔라"만 주면 빠져나올 방법을
        # 모른 채 그 run이 영구히 막힌다.
        (self.state / "runs").mkdir(parents=True)
        (self.state / "runs" / "wrecked.json").write_text("{partial", encoding="utf-8")
        completed, payload = self.invoke("--mode", "fresh", "--run-id", "wrecked")
        self.assertEqual(completed.returncode, 64)
        self.assertEqual(payload["status"], "run_id_collision")
        self.assertIsNone(payload["conflicting_run_id"])
        self.assertIn("손상", payload["hint"])
        self.assertIn("wrecked.json", payload["hint"])

    def test_a_real_collision_names_the_owner(self):
        self.invoke("--mode", "fresh", "--run-id", "a/b")
        _completed, payload = self.invoke("--mode", "fresh", "--run-id", "a b")
        self.assertIn("'a/b'", payload["hint"])

    def test_an_ordinary_run_id_is_unaffected(self):
        _completed, payload = self.invoke("--mode", "fresh", "--run-id", "normal-run")
        self.assertEqual(payload["status"], "ok")


if __name__ == "__main__":
    unittest.main()
