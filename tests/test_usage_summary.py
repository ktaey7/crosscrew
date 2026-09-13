"""Synthetic result envelopes only; no providers, state discovery, or services."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import usage_summary

ROOT = Path(__file__).resolve().parent.parent
MARKER = "DO_NOT_ECHO_SECRET"


def envelope(provider, status="ok", usage=None, **extra):
    value = {"schema_version": "2.0", "status": status, "exit_code": 0, "provider": provider,
             "stdout": MARKER, "usage": usage}
    value.update(extra)
    return value


def claude_usage(**tokens):
    return {"source": "claude.result.usage", "scope": "provider_terminal", "tokens": tokens}


def codex_usage(source="codex.turn.completed.usage", **tokens):
    return {"source": source, "scope": "provider_terminal", "tokens": tokens}


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, value, name=None, raw=None):
        self.n += 1
        path = self.dir / (name or f"{MARKER}-{self.n}.json")
        path.write_text(raw if raw is not None else json.dumps(value))
        return path

    def token(self, report, provider, field):
        cell = report["providers"][provider]["tokens"][field]
        return cell["reported_sum"], cell["missing_calls"]

    def test_mixed_success_failure_and_partial_counts(self):
        paths = [
            self.write(envelope("claude", usage=claude_usage(input_tokens=12, output_tokens=78))),
            self.write(envelope("claude", status="provider_error", usage=claude_usage(output_tokens=7))),
            self.write(envelope("claude", status="timeout")),  # usage null: every field missing
            self.write(envelope("claude", usage=claude_usage(input_tokens=0, cache_read_input_tokens=0))),
        ]
        report = usage_summary.summarize(paths)
        claude = report["providers"]["claude"]
        self.assertEqual((report["files"], report["invalid_files"]), (4, 0))
        self.assertEqual((claude["calls"], claude["failed_calls"]), (4, 2))
        self.assertEqual(self.token(report, "claude", "input_tokens"), (12, 2))
        self.assertEqual(self.token(report, "claude", "output_tokens"), (85, 2))
        self.assertEqual(self.token(report, "claude", "cache_read_input_tokens"), (0, 3))
        self.assertEqual(self.token(report, "claude", "cache_creation_input_tokens"), (None, 4))

    def test_invalid_count_fields_do_not_invalidate_the_call(self):
        bad = {"input_tokens": True, "cached_input_tokens": -1, "output_tokens": 1.5,
               "reasoning_output_tokens": 2**63}
        paths = [self.write(envelope("codex", usage=codex_usage(**bad))),
                 self.write(envelope("codex", status="provider_error",
                                     usage=codex_usage("codex.turn.failed.usage", input_tokens=5, output_tokens="9")))]
        report = usage_summary.summarize(paths)
        codex = report["providers"]["codex"]
        self.assertEqual((report["invalid_files"], codex["calls"], codex["failed_calls"]), (0, 2, 1))
        self.assertEqual(self.token(report, "codex", "input_tokens"), (None, 2))
        self.assertEqual(self.token(report, "codex", "output_tokens"), (None, 2))
        self.assertEqual(self.token(report, "codex", "reasoning_output_tokens"), (None, 2))
        self.assertEqual(self.token(report, "codex", "cached_input_tokens"), (None, 2))

    def test_wrong_provenance_is_missing_not_counted(self):
        paths = [
            self.write(envelope("claude", usage=claude_usage(input_tokens=1) | {"scope": "host_total"})),
            self.write(envelope("claude", usage=claude_usage(input_tokens=1) | {"source": "codex.turn.completed.usage"})),
            self.write(envelope("codex", usage=codex_usage("claude.result.usage", input_tokens=1))),
            self.write(envelope("codex", usage={"source": "codex.turn.completed.usage", "scope": "provider_terminal",
                                                "tokens": [1]})),
            self.write(envelope("codex", usage="12 tokens")),
        ]
        report = usage_summary.summarize(paths)
        self.assertEqual(report["invalid_files"], 0)
        self.assertEqual(self.token(report, "claude", "input_tokens"), (None, 2))
        self.assertEqual(self.token(report, "codex", "input_tokens"), (None, 3))

    def test_non_string_provenance_is_missing_without_crashing(self):
        weird = ({}, [], ["claude.result.usage"], {"a": 1}, None, 1, True, 1.5)
        for bad in weird:
            with self.subTest(source=bad):
                usage = {"scope": "provider_terminal", "source": bad, "tokens": {"input_tokens": 123}}
                self.assertEqual(usage_summary.trusted_tokens("claude", usage), {})
            with self.subTest(scope=bad):
                usage = {"scope": bad, "source": "claude.result.usage", "tokens": {"input_tokens": 123}}
                self.assertEqual(usage_summary.trusted_tokens("claude", usage), {})
        paths = [self.write(envelope("claude", usage={"scope": "provider_terminal", "source": {},
                                                      "tokens": {"input_tokens": 123}})),
                 self.write(envelope("codex", status="provider_error",
                                     usage={"scope": ["provider_terminal"], "source": ["codex.turn.failed.usage"],
                                            "tokens": {"output_tokens": 9}})),
                 self.write(envelope("claude", usage=claude_usage(input_tokens=1)))]
        report = usage_summary.summarize(paths)
        self.assertEqual(report["invalid_files"], 0)
        self.assertEqual((report["providers"]["claude"]["calls"], report["providers"]["codex"]["failed_calls"]), (2, 1))
        self.assertEqual(self.token(report, "claude", "input_tokens"), (1, 1))
        self.assertEqual(self.token(report, "codex", "output_tokens"), (None, 1))

    def test_provider_groups_stay_separate_and_grok_agy_have_no_tokens(self):
        paths = [self.write(envelope("claude", usage=claude_usage(input_tokens=10))),
                 self.write(envelope("codex", usage=codex_usage(input_tokens=20))),
                 self.write(envelope("grok", status="unavailable", usage=claude_usage(input_tokens=99))),
                 self.write(envelope("agy"))]
        report = usage_summary.summarize(paths)
        self.assertEqual(self.token(report, "claude", "input_tokens"), (10, 0))
        self.assertEqual(self.token(report, "codex", "input_tokens"), (None, 1))
        self.assertEqual(report["providers"]["grok"]["failed_calls"], 1)
        self.assertTrue(all(cell["reported_sum"] is None for cell in report["providers"]["grok"]["tokens"].values()))
        agy = report["providers"]["agy"]
        self.assertEqual((agy["calls"], agy["failed_calls"], agy["aggregation"]), (1, 0, "unverified"))
        self.assertTrue(all(cell["reported_sum"] is None for cell in agy["tokens"].values()))
        self.assertEqual(set(report["providers"]), {"claude", "codex", "grok", "agy"})
        self.assertEqual(tuple(report["providers"]["claude"]["tokens"]), usage_summary.provider_usage.FIELDS["claude"])
        self.assertEqual(tuple(report["providers"]["codex"]["tokens"]), usage_summary.provider_usage.FIELDS["codex"])
        self.assertNotIn("host_total", json.dumps(report))

    def test_old_envelope_without_usage_key_is_a_valid_call_with_missing_usage(self):
        value = envelope("claude")
        del value["usage"]
        report = usage_summary.summarize([self.write(value)])
        self.assertEqual(report["providers"]["claude"]["calls"], 1)
        self.assertEqual(self.token(report, "claude", "output_tokens"), (None, 1))

    def test_invalid_files_are_counted_and_do_not_block_valid_ones(self):
        fifo = self.dir / "pipe"
        os.mkfifo(fifo)
        big = self.write(None, raw=json.dumps(envelope("claude")) + " " * usage_summary.MAX_BYTES)
        exact = self.write(None, raw=json.dumps(envelope("codex")).ljust(usage_summary.MAX_BYTES))
        paths = [
            self.write(None, raw="{" + MARKER),                       # malformed JSON
            self.write(["not", "an", "object"]),                      # wrong shape
            self.write(envelope("openai")),                           # unsupported provider
            self.write(envelope("claude", status="")),                # empty status
            self.write(envelope("claude", status=0)),                 # non-string status
            self.write({"schema_version": "2.0", "status": "ok"}),    # provider missing
            self.dir / "missing.json",                                # unreadable
            self.dir,                                                 # non-regular: directory
            fifo,                                                     # non-regular: FIFO
            big,                                                      # over 2 MiB
            exact,                                                    # exactly 2 MiB stays valid
            self.write(envelope("claude", usage=claude_usage(output_tokens=3))),
        ]
        report = usage_summary.summarize(paths)
        self.assertEqual((report["files"], report["invalid_files"]), (12, 10))
        self.assertEqual(report["providers"]["claude"]["calls"], 1)
        self.assertEqual(report["providers"]["codex"]["calls"], 1)
        self.assertEqual(self.token(report, "claude", "output_tokens"), (3, 0))
        self.assertNotIn(MARKER, json.dumps(report))
        self.assertNotIn(str(self.dir), json.dumps(report))

    def test_accepts_path_like_and_string_paths(self):
        path = self.write(envelope("agy"))
        self.assertEqual(usage_summary.summarize([path])["files"], 1)
        self.assertEqual(usage_summary.summarize([str(path)])["files"], 1)

    def run_cli(self, *paths):
        return subprocess.run([sys.executable, str(ROOT / "usage_summary.py"), *map(str, paths)],
                              capture_output=True, text=True, cwd=self.dir)

    def test_cli_prints_only_the_json_report_and_exit_codes_follow_invalid_files(self):
        good = self.write(envelope("codex", usage=codex_usage(input_tokens=4, reasoning_output_tokens=1)))
        bad = self.write(None, raw="not json " + MARKER)
        ok = self.run_cli(good)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout), usage_summary.summarize([good]))
        self.assertEqual(ok.stdout.count("\n"), 1)
        self.assertEqual(ok.stderr, "")
        failed = self.run_cli(good, bad)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(json.loads(failed.stdout)["invalid_files"], 1)
        self.assertEqual(json.loads(failed.stdout)["providers"]["codex"]["calls"], 1)
        self.assertNotIn(MARKER, failed.stdout + failed.stderr)
        self.assertNotIn(str(bad), failed.stdout + failed.stderr)
        self.assertEqual(set(os.listdir(self.dir)), {good.name, bad.name})  # no disk writes

    def test_cli_requires_at_least_one_file(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
