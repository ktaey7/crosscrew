"""Synthetic official-format fixtures; no providers, credentials, or services."""
import json
from pathlib import Path
import shlex
import unittest

import call_worker
import provider_usage
from tests import test_call_worker

FIXTURES = Path(__file__).parent / "fixtures" / "usage"
SID = "11223344-5566-7788-99aa-bbccddeeff00"


class UsageParserTests(unittest.TestCase):
    def test_claude_retains_final_text_session_and_only_token_fields(self):
        text, sid, usage, error, invalid = provider_usage.claude_result((FIXTURES / "claude-success.json").read_text())
        self.assertEqual((text, sid, error, invalid), ("FINAL_CLAUDE\n", SID, False, None))
        self.assertEqual(usage["tokens"], {"input_tokens": 12, "cache_creation_input_tokens": 34,
                                         "cache_read_input_tokens": 56, "output_tokens": 78})
        self.assertNotIn("DO_NOT_COPY", json.dumps(usage))
        self.assertNotIn("total_cost_usd", usage)

    def test_missing_is_unknown_and_explicit_zero_is_preserved(self):
        self.assertIsNone(provider_usage.usage_record("claude", {}, "fixture"))
        record = provider_usage.usage_record("claude", {"input_tokens": 0}, "fixture")
        self.assertEqual(record["tokens"]["input_tokens"], 0)
        self.assertIsNone(record["tokens"]["output_tokens"])

    def test_invalid_counts_are_not_coerced_or_echoed(self):
        for bad in (True, -1, 1.5, "123", 2**63, {}, [], float("inf")):
            with self.subTest(value=bad):
                self.assertIsNone(provider_usage.usage_record("codex", {"input_tokens": bad}, "fixture"))

    def test_partial_malformed_or_wrong_shaped_json_is_not_a_final_answer(self):
        for raw in ('{"type":"result","result":"unfinished', '[]', 'null', '{"type":"assistant"}', '[' * 1200):
            with self.subTest(raw=raw[:40]):
                text, sid, usage, _, invalid = provider_usage.claude_result(raw)
                self.assertEqual(text, "")
                self.assertIsNone(usage)
                self.assertIsNotNone(invalid)

    def test_plain_text_compatibility_has_no_invented_usage(self):
        self.assertEqual(provider_usage.claude_result("legacy answer"), ("legacy answer", None, None, False, None))

    def test_failed_claude_retains_reported_partial_usage(self):
        text, _, usage, error, _ = provider_usage.claude_result((FIXTURES / "claude-error.json").read_text())
        self.assertTrue(error)
        self.assertNotIn("DO_NOT_COPY", text)
        self.assertEqual(usage["tokens"]["output_tokens"], 7)
        self.assertIsNone(usage["tokens"]["cache_read_input_tokens"])

    def test_codex_terminal_usage_is_not_summed_twice(self):
        raw = (FIXTURES / "codex-terminal.jsonl").read_text()
        usage = provider_usage.codex_usage(raw + raw)
        self.assertEqual(usage["tokens"]["input_tokens"], 120)
        self.assertEqual(usage["tokens"]["cached_input_tokens"], 100)
        self.assertNotIn("DO_NOT_COPY", json.dumps(usage))

    def test_later_terminal_without_usage_clears_earlier_usage(self):
        raw = (FIXTURES / "codex-terminal.jsonl").read_text()
        self.assertIsNone(provider_usage.codex_usage(raw + '{"type":"turn.failed"}'))

    def test_codex_malformed_stream_events_do_not_crash_or_invent_totals(self):
        raw = 'null\n[]\n{"item":5}\n{"type":[]}\n{"type":"thread.started","thread":5}\n{"type":"turn.completed","usage":'
        self.assertIsNone(provider_usage.codex_usage(raw))
        self.assertEqual(call_worker.codex_result(raw), ("", None))


class UsageAdapterTests(unittest.TestCase):
    setUp = test_call_worker.AdapterTests.setUp
    tearDown = test_call_worker.AdapterTests.tearDown
    fake = test_call_worker.AdapterTests.fake
    invoke = test_call_worker.AdapterTests.invoke
    environment = test_call_worker.AdapterTests.environment

    def fixture_cli(self, provider, fixture, exit_code=0):
        self.fake(provider, "cat " + shlex.quote(str(FIXTURES / fixture)) + f"\nexit {exit_code}")

    def test_claude_json_final_text_and_observed_session_reach_envelope(self):
        self.fixture_cli("claude", "claude-success.json")
        proc, result = self.invoke("claude")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(result["stdout"], "FINAL_CLAUDE\n")
        self.assertEqual(result["session_id"], SID)
        self.assertEqual(result["usage"]["source"], "claude.result.usage")
        self.assertNotIn("DO_NOT_COPY", json.dumps(result))

    def test_claude_error_flag_cannot_become_success_even_with_exit_zero(self):
        self.fixture_cli("claude", "claude-error.json")
        proc, result = self.invoke("claude")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotEqual(result["status"], "ok")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["usage"]["tokens"]["output_tokens"], 7)

    def test_claude_process_error_retains_usage(self):
        self.fixture_cli("claude", "claude-error.json", 1)
        proc, result = self.invoke("claude")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIsNotNone(result["usage"])

    def test_claude_partial_json_is_explicit_invalid_output(self):
        self.fake("claude", "printf '%s' '{\"type\":\"result\",\"result\":'")
        proc, result = self.invoke("claude")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(result["status"], "invalid_output")
        self.assertIsNone(result["usage"])
        self.assertEqual(result["stdout"], "")

    def test_mismatched_session_does_not_overwrite_expected_identity(self):
        self.fixture_cli("claude", "claude-success.json")
        expected = "22334455-6677-8899-aabb-ccddeeff0011"
        proc, result = self.invoke("claude", "--mode", "fresh", "--session-id", expected)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(result["session_id"], expected)
        self.assertEqual(result["provider_output_error"], "session_mismatch")

    def test_codex_all_modes_keep_terminal_usage_and_final_body(self):
        self.fixture_cli("codex", "codex-terminal.jsonl")
        for mode in ("oneshot", "fresh", "resume"):
            with self.subTest(mode=mode):
                proc, result = self.invoke("codex", "--mode", mode, "--run-id", "usage-codex")
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(result["stdout"], "FINAL_CODEX")
                self.assertEqual(result["usage"]["tokens"]["input_tokens"], 120)

    def test_unsupported_provider_usage_is_null(self):
        self.fake("grok", "printf 'GROK_OK'")
        proc, result = self.invoke("grok")
        self.assertEqual(proc.returncode, 0)
        self.assertIsNone(result["usage"])

    def test_partial_codex_events_without_final_message_are_not_success(self):
        self.fake("codex", "printf '%s' '{\"type\":\"turn.completed\",\"usage\":'")
        proc, result = self.invoke("codex")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(result["status"], "empty_output")
        self.assertIsNone(result["usage"])


if __name__ == "__main__":
    unittest.main()
