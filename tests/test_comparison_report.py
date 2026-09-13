"""Synthetic contract tests; no live results or external services are used."""
import copy
import io
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

import comparison_report
import usage_summary


PYTHON = sys.executable
SCRIPT = Path(comparison_report.__file__).resolve()
PRIVATE = "synthetic-private-marker-sk-not-a-real-credential"


class ComparisonReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="comparison-report-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.counter = 0
        self.direct = self.envelope("direct.json", 100, 20, 30)
        self.delegated = self.envelope("delegated.json", 60, 10, 20)
        self.spec = {
            "schema_version": 1,
            "direct": {"accepted": True, "host_results": [self.direct], "worker_results": []},
            "delegated": {"accepted": True, "host_results": [self.delegated], "worker_results": []},
        }

    def envelope(self, name, input_tokens=100, cached=20, output=30,
                 provider="codex", status="ok", session=None, tokens=None):
        self.counter += 1
        sources = {"codex": "codex.turn.completed.usage", "claude": "claude.result.usage",
                   "grok": "grok.session.usage.json", "agy": "agy.json.usage"}
        value = {
            "schema_version": "2.0", "provider": provider, "status": status,
            "run_id": f"{PRIVATE}-run-{self.counter}",
            "session_id": session or str(uuid.UUID(int=self.counter)),
            "started_at": f"2026-01-01T00:00:{self.counter:02d}+00:00",
            "stdout": PRIVATE, "stderr": PRIVATE, "messages": [{"body": PRIVATE}],
            "usage": {"source": sources[provider], "scope": "provider_terminal",
                      "tokens": tokens if tokens is not None else {
                          "input_tokens": input_tokens, "cached_input_tokens": cached,
                          "output_tokens": output}},
        }
        self.write(name, value)
        return name

    def write(self, name, value):
        (self.base / name).write_text(json.dumps(value), encoding="utf-8")

    def change(self, name, update):
        value = json.loads((self.base / name).read_text(encoding="utf-8"))
        update(value)
        self.write(name, value)

    def report(self):
        return comparison_report.build_report(self.spec, self.base)

    def assert_unknown_savings(self, report):
        self.assertEqual(report["savings"], {
            "uncached_input_tokens": {"absolute": None, "percent": None},
            "cached_input_tokens": {"absolute": None, "percent": None},
            "output_tokens": {"absolute": None, "percent": None},
        })

    def assert_incomplete(self, report, arm="direct"):
        self.assertFalse(report["eligible"])
        self.assertIn(f"{arm}_host_usage_incomplete", report["reasons"])
        self.assertEqual(report["arms"][arm]["codex_tokens"], {
            "uncached_input_tokens": None, "cached_input_tokens": None, "output_tokens": None,
        })
        self.assert_unknown_savings(report)

    def cli(self, args, cwd=None):
        return subprocess.run([PYTHON, str(SCRIPT), *map(str, args)],
                              cwd=cwd or self.base, capture_output=True, text=True,
                              timeout=10, check=False)

    def assert_cli_error(self, result):
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '{"error":"invalid_manifest"}\n')
        self.assertEqual(result.stderr, "")

    def test_complete_vectors_and_separate_savings(self):
        result = self.report()
        self.assertEqual(set(result), {"schema_version", "eligible", "reasons", "arms", "savings"})
        self.assertEqual(result["schema_version"], 1)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["arms"]["direct"]["codex_tokens"], {
            "uncached_input_tokens": 80, "cached_input_tokens": 20, "output_tokens": 30,
        })
        self.assertEqual(result["savings"], {
            "uncached_input_tokens": {"absolute": 30, "percent": 37.5},
            "cached_input_tokens": {"absolute": 10, "percent": 50.0},
            "output_tokens": {"absolute": 10, "percent": 33.333333},
        })

    def test_four_frozen_summaries_preserved_and_manifest_unchanged(self):
        self.spec["private_extra"] = {"body": PRIVATE}
        self.spec["direct"]["private_extra"] = PRIVATE
        before = copy.deepcopy(self.spec)
        with mock.patch.object(usage_summary, "summarize", wraps=usage_summary.summarize) as summarize:
            result = self.report()
        self.assertEqual(summarize.call_count, 4)
        self.assertEqual(summarize.call_args_list, [
            mock.call([self.base / self.direct]), mock.call([]),
            mock.call([self.base / self.delegated]), mock.call([]),
        ])
        for arm in ("direct", "delegated"):
            for key, role in (("host", "host_results"), ("workers", "worker_results")):
                self.assertEqual(result["arms"][arm][key], usage_summary.summarize(
                    [self.base / path for path in self.spec[arm][role]]))
        self.assertEqual(self.spec, before)

    def test_reasoning_missing_does_not_invalidate_or_add_to_output(self):
        self.assertTrue(self.report()["eligible"])
        self.change(self.direct, lambda value: value["usage"]["tokens"].update(reasoning_output_tokens=9))
        result = self.report()
        self.assertTrue(result["eligible"])
        self.assertEqual(result["arms"]["direct"]["codex_tokens"]["output_tokens"], 30)

    def test_resume_uses_one_cumulative_snapshot_and_deduplicates_files(self):
        session = json.loads((self.base / self.direct).read_text())["session_id"]
        resumed = self.envelope("resume.json", 160, 40, 50, session=session)
        self.spec["direct"]["host_results"] = [resumed, self.direct, self.direct]
        result = self.report()
        self.assertTrue(result["eligible"])
        host = result["arms"]["direct"]["host"]
        self.assertEqual((host["files"], host["duplicate_files"]), (3, 1))
        codex = host["providers"]["codex"]
        self.assertEqual((codex["calls"], codex["sessions"], codex["cumulative_calls"]), (2, 1, 2))
        self.assertEqual(result["arms"]["direct"]["codex_tokens"], {
            "uncached_input_tokens": 120, "cached_input_tokens": 40, "output_tokens": 50,
        })

    def test_equal_resume_and_copied_envelope_do_not_double_count(self):
        session = json.loads((self.base / self.direct).read_text())["session_id"]
        resumed = self.envelope("equal.json", session=session)
        (self.base / "copy.json").write_bytes((self.base / self.direct).read_bytes())
        self.spec["direct"]["host_results"] += [resumed, "copy.json"]
        result = self.report()
        self.assertTrue(result["eligible"])
        self.assertEqual(result["arms"]["direct"]["host"]["duplicate_files"], 1)
        self.assertEqual(result["arms"]["direct"]["codex_tokens"]["uncached_input_tokens"], 80)

    def test_independent_sessions_are_aggregated_by_summary(self):
        self.spec["direct"]["host_results"].append(self.envelope("independent.json", 40, 5, 7))
        result = self.report()
        self.assertTrue(result["eligible"])
        self.assertEqual(result["arms"]["direct"]["codex_tokens"], {
            "uncached_input_tokens": 115, "cached_input_tokens": 25, "output_tokens": 37,
        })

    def test_later_complete_snapshot_covers_earlier_missing_field(self):
        self.change(self.direct, lambda value: value["usage"]["tokens"].pop("output_tokens"))
        session = json.loads((self.base / self.direct).read_text())["session_id"]
        self.spec["direct"]["host_results"].append(self.envelope("resume.json", 160, 40, 50, session=session))
        self.assertTrue(self.report()["eligible"])

    def test_counter_reset_and_incompatible_snapshots_are_unresolved(self):
        session = json.loads((self.base / self.direct).read_text())["session_id"]
        original = (self.base / self.direct).read_bytes()
        for earlier, later in ((None, {"input_tokens": 90, "cached_input_tokens": 20, "output_tokens": 30}),
                               ({"input_tokens": 100}, {"cached_input_tokens": 20, "output_tokens": 30})):
            with self.subTest(earlier=earlier):
                (self.base / self.direct).write_bytes(original)
                if earlier is not None:
                    self.change(self.direct, lambda value: value["usage"].update(tokens=earlier))
                resumed = self.envelope("reset.json", session=session, tokens=later)
                self.spec["direct"]["host_results"] = [self.direct, resumed]
                result = self.report()
                self.assert_incomplete(result)
                self.assertEqual(result["arms"]["direct"]["host"]["providers"]["codex"]["unresolved_sessions"], 1)

    def test_missing_or_invalid_required_host_counts(self):
        original = (self.base / self.direct).read_bytes()
        for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
            for value in (None, True, -1, 1.5, "100", 2**63):
                with self.subTest(field=field, value=value):
                    (self.base / self.direct).write_bytes(original)
                    self.change(self.direct, lambda row: row["usage"]["tokens"].update({field: value}))
                    self.assert_incomplete(self.report())
            (self.base / self.direct).write_bytes(original)
            self.change(self.direct, lambda row: row["usage"]["tokens"].pop(field))
            self.assert_incomplete(self.report())

    def test_missing_untrusted_usage_and_session_identity(self):
        original = (self.base / self.direct).read_bytes()
        changes = [lambda row: row.pop("usage"),
                   lambda row: row["usage"].update(scope="unknown"),
                   lambda row: row["usage"].update(source=PRIVATE),
                   lambda row: row["usage"].update(aggregation="invocation"),
                   lambda row: row.pop("session_id"),
                   lambda row: row.update(session_id=PRIVATE)]
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                (self.base / self.direct).write_bytes(original)
                self.change(self.direct, change)
                self.assert_incomplete(self.report())

    def test_failed_codex_and_mixed_or_non_codex_hosts(self):
        original = copy.deepcopy(self.spec)
        for provider in ("codex", "claude", "grok", "agy"):
            with self.subTest(provider=provider):
                self.spec = copy.deepcopy(original)
                extra = self.envelope(f"{provider}.json", provider=provider,
                                      status="failed" if provider == "codex" else "ok")
                self.spec["direct"]["host_results"].append(extra)
                self.assert_incomplete(self.report())
                self.spec["direct"]["host_results"] = [extra]
                self.assert_incomplete(self.report())

    def test_result_errors_remain_in_summary(self):
        (self.base / "invalid.json").write_text("{broken " + PRIVATE)
        (self.base / "directory").mkdir()
        self.write("bad-envelope.json", {"provider": "codex", "status": ""})
        for name in ("missing.json", "invalid.json", "directory", "bad-envelope.json", "bad\0path"):
            with self.subTest(name=name):
                self.spec["direct"]["host_results"] = [self.direct, name]
                result = self.report()
                self.assert_incomplete(result)
                self.assertEqual(result["arms"]["direct"]["host"]["invalid_files"], 1)

    def test_cached_must_be_subset_and_equal_is_valid(self):
        self.change(self.direct, lambda row: row["usage"]["tokens"].update(cached_input_tokens=101))
        self.assert_incomplete(self.report())
        self.change(self.direct, lambda row: row["usage"]["tokens"].update(cached_input_tokens=100))
        result = self.report()
        self.assertTrue(result["eligible"])
        self.assertEqual(result["savings"]["uncached_input_tokens"], {"absolute": -50, "percent": None})

    def test_unreadable_results_remain_invalid_summary_files(self):
        original_open = os.open

        def restricted_open(path, *args, **kwargs):
            if Path(path) == self.base / self.direct:
                raise PermissionError(PRIVATE)
            return original_open(path, *args, **kwargs)

        with mock.patch.object(usage_summary.os, "open", side_effect=restricted_open):
            result = self.report()
        self.assert_incomplete(result)
        self.assertEqual(result["arms"]["direct"]["host"]["invalid_files"], 1)

    def test_api_does_not_write_files(self):
        before = {path.name: path.read_bytes() for path in self.base.iterdir()}
        self.report()
        after = {path.name: path.read_bytes() for path in self.base.iterdir()}
        self.assertEqual(after, before)

    def test_rejected_quality_retains_vectors_but_suppresses_savings(self):
        self.spec["delegated"]["accepted"] = False
        result = self.report()
        self.assertEqual(result["reasons"], ["delegated_not_accepted"])
        self.assertEqual(result["arms"]["direct"]["codex_tokens"]["output_tokens"], 30)
        self.assertEqual(result["arms"]["delegated"]["codex_tokens"]["output_tokens"], 20)
        self.assert_unknown_savings(result)

    def test_missing_and_unverified_worker_tokens_do_not_block_comparison(self):
        absent = self.envelope("absent.json", provider="claude")
        self.change(absent, lambda row: row.pop("usage"))
        unverified = self.envelope("unverified.json", provider="agy")
        no_session = self.envelope("worker-codex.json")
        self.change(no_session, lambda row: row.pop("session_id"))
        self.spec["delegated"]["worker_results"] = [absent, unverified, no_session]
        result = self.report()
        self.assertTrue(result["eligible"])
        workers = result["arms"]["delegated"]["workers"]["providers"]
        self.assertEqual(workers["agy"]["aggregation"], "unverified")
        for provider in ("claude", "agy", "codex"):
            self.assertEqual(workers[provider]["tokens"]["input_tokens"],
                             {"reported_sum": None, "missing_calls": 1})

    def test_invalid_and_failed_workers_suppress_savings_keep_host_vectors(self):
        for provider in ("codex", "claude", "grok", "agy"):
            with self.subTest(provider=provider):
                failed = self.envelope("failed.json", provider=provider, status="error")
                self.spec["delegated"]["worker_results"] = ["missing.json", failed]
                result = self.report()
                self.assertEqual(result["reasons"], ["delegated_worker_result_invalid", "delegated_worker_failed"])
                self.assertEqual(result["arms"]["delegated"]["codex_tokens"]["output_tokens"], 20)
                self.assert_unknown_savings(result)

    def test_all_reasons_have_exact_stable_order(self):
        for arm in ("direct", "delegated"):
            self.spec[arm]["accepted"] = False
            self.spec[arm]["host_results"].append(f"{arm}-missing-host.json")
            failed = self.envelope(f"{arm}-failed.json", provider="claude", status="failed")
            self.spec[arm]["worker_results"] = [f"{arm}-missing-worker.json", failed]
        self.assertEqual(self.report()["reasons"], [
            "direct_not_accepted", "delegated_not_accepted",
            "direct_host_usage_incomplete", "delegated_host_usage_incomplete",
            "direct_worker_result_invalid", "delegated_worker_result_invalid",
            "direct_worker_failed", "delegated_worker_failed",
        ])

    def test_negative_savings_and_zero_denominators(self):
        self.change(self.direct, lambda row: row["usage"]["tokens"].update(
            input_tokens=20, cached_input_tokens=5, output_tokens=10))
        result = self.report()
        self.assertEqual(result["savings"], {
            "uncached_input_tokens": {"absolute": -35, "percent": -233.333333},
            "cached_input_tokens": {"absolute": -5, "percent": -100.0},
            "output_tokens": {"absolute": -10, "percent": -100.0},
        })
        self.change(self.direct, lambda row: row["usage"]["tokens"].update(
            input_tokens=0, cached_input_tokens=0, output_tokens=0))
        result = self.report()
        self.assertTrue(result["eligible"])
        self.assertEqual(result["savings"], {
            "uncached_input_tokens": {"absolute": -50, "percent": None},
            "cached_input_tokens": {"absolute": -10, "percent": None},
            "output_tokens": {"absolute": -20, "percent": None},
        })
        self.change(self.delegated, lambda row: row["usage"]["tokens"].update(
            input_tokens=0, cached_input_tokens=0, output_tokens=0))
        result = self.report()
        self.assertTrue(result["eligible"])
        for cell in result["savings"].values():
            self.assertEqual(cell, {"absolute": 0, "percent": None})

    def test_all_cross_list_overlaps_are_invalid_before_summary(self):
        lists = list(itertools.product(("direct", "delegated"), ("host_results", "worker_results")))
        for first, second in itertools.combinations(lists, 2):
            spec = copy.deepcopy(self.spec)
            spec[first[0]][first[1]].append("shared-missing.json")
            spec[second[0]][second[1]].append("shared-missing.json")
            with self.subTest(first=first, second=second):
                with mock.patch.object(usage_summary, "summarize") as summarize:
                    with self.assertRaises(ValueError):
                        comparison_report.build_report(spec, self.base)
                    summarize.assert_not_called()

    def test_absolute_dot_symlink_and_hardlink_aliases_overlap(self):
        (self.base / "nested").mkdir()
        (self.base / "symlink.json").symlink_to(self.base / self.direct)
        (self.base / "hardlink.json").hardlink_to(self.base / self.direct)
        for alias in (str(self.base / self.direct), "./direct.json", "nested/../direct.json",
                      "symlink.json", "hardlink.json"):
            with self.subTest(alias=alias):
                self.spec["delegated"]["worker_results"] = [alias]
                with self.assertRaises(ValueError):
                    self.report()

    def test_missing_path_alias_overlap_and_same_list_repetition(self):
        self.spec["direct"]["worker_results"] = ["missing.json", "missing.json"]
        result = self.report()
        self.assertEqual(result["reasons"], ["direct_worker_result_invalid"])
        self.spec["delegated"]["worker_results"] = [str(self.base / "missing.json")]
        with self.assertRaises(ValueError):
            self.report()

    def test_same_list_alias_uses_existing_deduplication(self):
        self.spec["direct"]["host_results"] += [str(self.base / self.direct), "./direct.json"]
        result = self.report()
        self.assertTrue(result["eligible"])
        self.assertEqual(result["arms"]["direct"]["host"]["duplicate_files"], 2)

    def test_symlink_result_keeps_frozen_reader_rules(self):
        (self.base / "symlink.json").symlink_to(self.base / self.direct)
        self.spec["direct"]["host_results"] = ["symlink.json"]
        result = self.report()
        self.assert_incomplete(result)
        self.assertEqual(result["arms"]["direct"]["host"]["invalid_files"], 1)

    def test_invalid_manifest_shapes_raise_value_error(self):
        cases = [None, [], "manifest", {}, {"schema_version": 1}]
        for version in (True, False, 1.0, "1", None, 0, 2):
            spec = copy.deepcopy(self.spec)
            spec["schema_version"] = version
            cases.append(spec)
        spec = copy.deepcopy(self.spec)
        del spec["schema_version"]
        cases.append(spec)
        for arm in ("direct", "delegated"):
            for value in (None, [], "arm"):
                spec = copy.deepcopy(self.spec)
                spec[arm] = value
                cases.append(spec)
            spec = copy.deepcopy(self.spec)
            del spec[arm]
            cases.append(spec)
            for field, values in {
                "accepted": [None, 0, 1, "true", []],
                "host_results": [None, [], "direct.json", (), [""], [None], [3], [Path("direct.json")]],
                "worker_results": [None, "worker.json", (), [""], [None], [False]],
            }.items():
                spec = copy.deepcopy(self.spec)
                del spec[arm][field]
                cases.append(spec)
                for value in values:
                    spec = copy.deepcopy(self.spec)
                    spec[arm][field] = value
                    cases.append(spec)
        for index, spec in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(ValueError):
                    comparison_report.build_report(spec, self.base)

    def test_cli_resolves_manifest_relative_paths_from_another_cwd(self):
        self.write("manifest.json", self.spec)
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        result = self.cli(["../manifest.json"], cwd=elsewhere)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(json.loads(result.stdout), self.report())
        self.spec["delegated"]["host_results"] = [str(self.base / self.delegated)]
        self.assertTrue(self.report()["eligible"])

    def test_cli_ineligible_exit_one_and_result_error_not_manifest_error(self):
        self.spec["direct"]["host_results"].append("missing.json")
        self.write("manifest.json", self.spec)
        result = self.cli([self.base / "manifest.json"])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), self.report())

    def test_cli_malformed_missing_unreadable_manifest_and_argument_errors(self):
        (self.base / "invalid.json").write_text("{broken " + PRIVATE)
        (self.base / "bad-encoding.json").write_bytes(b"\xff")
        self.write("bad-shape.json", {"schema_version": True, "private": PRIVATE})
        self.spec["delegated"]["host_results"] = [self.direct]
        self.write("overlap.json", self.spec)
        for args in ([], ["a", "b"], [PRIVATE], [self.base], ["invalid.json"],
                     ["bad-encoding.json"], ["bad-shape.json"], ["overlap.json"]):
            with self.subTest(args=args):
                self.assert_cli_error(self.cli(args))
        with mock.patch.object(comparison_report.os, "open", side_effect=PermissionError(PRIVATE)):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                    self.assertEqual(comparison_report.main(["unreadable.json"]), 2)
        self.assertEqual(out.getvalue(), '{"error":"invalid_manifest"}\n')
        self.assertEqual(err.getvalue(), "")

    def test_output_privacy_even_with_nested_extras_and_result_bodies(self):
        self.spec["secret"] = {"messages": [{"text": PRIVATE}], "run_id": PRIVATE}
        self.spec["direct"]["stdout"] = PRIVATE
        self.write("manifest.json", self.spec)
        result = self.cli([self.base / "manifest.json"])
        self.assertEqual(result.returncode, 0)
        rendered = result.stdout
        for forbidden in (PRIVATE, str(self.base), self.direct, self.delegated,
                          "stdout", "stderr", "session_id", "run_id", "messages", "secret"):
            self.assertNotIn(forbidden, rendered)
        for name in (self.direct, self.delegated):
            envelope = json.loads((self.base / name).read_text())
            self.assertNotIn(envelope["session_id"], rendered)
            self.assertNotIn(envelope["run_id"], rendered)
        self.assertEqual(json.loads(rendered), self.report())

    def test_cli_rejects_non_json_numeric_constants_even_in_ignored_fields(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                text = json.dumps(self.spec)[:-1] + ', "extra": ' + constant + '}'
                (self.base / "non-json.json").write_text(text)
                self.assert_cli_error(self.cli(["non-json.json"]))

    def test_cli_nonregular_manifest_does_not_wait(self):
        fifo = self.base / "manifest.fifo"
        os.mkfifo(fifo)
        self.assert_cli_error(self.cli([fifo]))


if __name__ == "__main__":
    unittest.main()
