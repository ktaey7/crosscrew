import json
import sys
import unittest

import agy_response
import usage_summary
from tests import test_call_worker as adapter_tests

SID = "13c729ce-046c-4ee1-a6ca-2b8b47007c45"
OTHER = "3b3d1792-4f41-4979-9d26-47b3370b5d86"


class ResponseTests(unittest.TestCase):
    def parse(self, **changes):
        row = dict(status="SUCCESS", response="ANSWER", conversation_id=SID)
        row.update(changes)
        return agy_response.parse_agy_response(json.dumps(row))

    def test_success_allowlists_observed_metadata(self):
        row = self.parse(conversation_id=SID.upper(), private_metadata="SECRET",
                         usage=dict(input_tokens=4, output_tokens=0, total_tokens=99,
                                    thinking_tokens=True, cache_read_tokens=-1, private="SECRET"))
        self.assertEqual((row["status"], row["stdout"], row["session_id"]), ("ok", "ANSWER", SID))
        self.assertEqual(row["usage"]["aggregation"], "unverified")
        self.assertEqual(row["usage"]["tokens"], dict(input_tokens=4, output_tokens=0,
                         total_tokens=99, thinking_tokens=None, cache_read_tokens=None))
        self.assertNotIn("SECRET", json.dumps(row))

    def test_permission_denial_never_promotes_partial_answer(self):
        row = self.parse(denied_actions=[{"tool": "write_file", "detail": "SECRET"}])
        self.assertEqual((row["status"], row["stdout"]), ("tool_permission_denied", ""))
        self.assertNotIn("SECRET", json.dumps(row))

    def test_errors_empty_and_invalid_shapes(self):
        for raw, expected in (("", "empty_output"), ("  ", "empty_output"),
                              ("{bad", "invalid_output"), ("[bad", "invalid_output"),
                              ("[]", "invalid_output"), ("null", "invalid_output"),
                              ('{"status":"SUCCESS"}', "invalid_output")):
            with self.subTest(raw=raw):
                self.assertEqual(agy_response.parse_agy_response(raw)["status"], expected)
        self.assertEqual(self.parse(status="ERROR")["status"], "error")
        self.assertEqual(self.parse(response=" \n")["status"], "empty_output")
        self.assertEqual(agy_response.parse_agy_response("legacy answer")["stdout"], "legacy answer")

    def test_invalid_ids_and_usage_are_not_fabricated(self):
        for bad in (None, "", "bad", 3, [], {}):
            with self.subTest(bad=bad):
                row = self.parse(conversation_id=bad, usage=dict(input_tokens=2**63))
                self.assertIsNone(row["session_id"])
                self.assertIsNone(row["usage"])


class SessionTests(unittest.TestCase):
    setUp = adapter_tests.AdapterTests.setUp
    tearDown = adapter_tests.AdapterTests.tearDown
    fake = adapter_tests.AdapterTests.fake
    environment = adapter_tests.AdapterTests.environment
    invoke = adapter_tests.AdapterTests.invoke

    def fake_json(self, **changes):
        row = dict(status="SUCCESS", response="ANSWER", conversation_id=SID)
        row.update(changes)
        self.fake("agy", "printf '%s\\n' '" + json.dumps(row) + "'")

    def test_fresh_captures_id_and_implicit_resume_passes_native_conversation(self):
        self.fake_json(usage=dict(input_tokens=7))
        completed, first = self.invoke("agy", "--mode", "fresh", "--run-id", "continuity")
        self.assertEqual(completed.returncode, 0, first)
        self.assertEqual(first["session_id"], SID)
        program = self.bin / "agy"
        program.write_text(f"#!{sys.executable}\n" +
            "import json,sys\na=sys.argv[1:]\n" +
            "assert a[a.index('--output-format')+1]=='json'\n" +
            "assert '--disable-slash-commands' not in a\n" +
            f"assert a[a.index('--conversation')+1]=={SID!r}\n" +
            f"print(json.dumps(dict(status='SUCCESS',response='RESUMED',conversation_id={SID!r})))\n")
        completed, resumed = self.invoke("agy", "--mode", "resume", "--run-id", "continuity")
        self.assertEqual(completed.returncode, 0, resumed)
        self.assertEqual((resumed["stdout"], resumed["session_id"]), ("RESUMED", SID))

    def test_fresh_or_resume_missing_id_and_wrong_id_fail_closed(self):
        for mode, sid, expected in (("fresh", None, "session_id_missing"),
                                    ("resume", None, "session_id_missing"),
                                    ("resume", OTHER, "session_mismatch")):
            self.fake_json(conversation_id=sid)
            extra = ["--mode", mode]
            if mode == "resume":
                extra += ["--session-id", SID]
            with self.subTest(mode=mode, sid=sid):
                completed, row = self.invoke("agy", *extra)
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(row["status"], "invalid_output")
                self.assertEqual(row["provider_output_error"], expected)
                self.assertNotEqual(row["session_id"], OTHER)

    def test_tool_denied_with_success_and_partial_answer_is_failure(self):
        self.fake_json(denied_actions=["shell"])
        completed, row = self.invoke("agy", "--mode", "fresh")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual((row["status"], row["stdout"], row["retryable"]), ("tool_permission_denied", "", False))

    def test_native_auth_and_timeout_keep_precedence_over_bad_json(self):
        self.fake("agy", "printf '{bad'; printf 'authentication required' >&2; exit 1")
        _, row = self.invoke("agy", "--mode", "fresh")
        self.assertEqual(row["status"], "auth_expired")
        self.fake("agy", "printf '{bad'; sleep 2")
        _, row = self.invoke("agy", "--mode", "fresh", "--timeout", "0.1")
        self.assertEqual(row["status"], "timeout")

    def test_implicit_resume_rejects_different_target_before_provider_launch(self):
        self.fake_json()
        _, first = self.invoke("agy", "--mode", "fresh", "--run-id", "target-bound")
        self.assertEqual(first["status"], "ok")
        other_target = self.base / "other"
        other_target.mkdir()
        self.fake("agy", "printf 'SHOULD_NOT_RUN'")
        _, row = self.invoke("agy", "--mode", "resume", "--run-id", "target-bound", "--target", str(other_target))
        self.assertEqual(row["status"], "target_mismatch")
        self.assertNotIn("SHOULD_NOT_RUN", json.dumps(row))

    def test_unknown_usage_scope_is_never_summed(self):
        envelope = dict(schema_version="2.0", provider="agy", status="ok", session_id=SID,
                        usage=agy_response.parse_agy_response(json.dumps(dict(
                            status="SUCCESS", response="OK", usage=dict(input_tokens=100))))["usage"])
        paths = []
        for index in range(2):
            path = self.base / f"result-{index}.json"
            path.write_text(json.dumps(envelope))
            paths.append(path)
        report = usage_summary.summarize(paths)["providers"]["agy"]
        self.assertEqual(report["aggregation"], "unverified")
        self.assertTrue(all(cell["reported_sum"] is None for cell in report["tokens"].values()))
