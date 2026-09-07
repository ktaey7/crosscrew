import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import call_worker  # noqa: E402
import worker_job  # noqa: E402

CALL_WORKER = ROOT / "call_worker.py"


class RouteResolutionTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "backends.json").read_text(encoding="utf-8"))

    def route(self, host, provider, profile="review", mode="oneshot"):
        return call_worker.route_for(self.config, host, provider, profile, mode)

    def test_dispatcher_and_supervisor_agree_on_every_route(self):
        for host in self.config["host_matrix"]:
            for provider in self.config["providers"]:
                for profile in ("review", "work", "research"):
                    self.assertEqual(
                        call_worker.route_for(self.config, host, provider, profile, "oneshot"),
                        worker_job.route_for(self.config, host, provider, profile, "oneshot"),
                        f"{host}->{provider}/{profile}",
                    )

    def test_shell_host_is_direct_for_all_providers(self):
        for provider in self.config["providers"]:
            self.assertEqual(self.route("shell", provider), "direct")

    def test_sandboxed_hosts_route_foreign_providers_through_the_broker(self):
        self.assertEqual(self.route("codex", "claude"), "broker")
        self.assertEqual(self.route("codex", "grok"), "broker")
        self.assertEqual(self.route("grok", "claude"), "broker")
        self.assertEqual(self.route("grok", "codex"), "broker")
        self.assertEqual(self.route("grok", "agy"), "broker")
        self.assertEqual(self.route("agy", "claude"), "broker")
        self.assertEqual(self.route("agy", "codex"), "broker")
        self.assertEqual(self.route("agy", "grok"), "broker")

    def test_claude_host_reaches_grok_and_agy_directly(self):
        self.assertEqual(self.route("claude", "grok"), "direct")
        self.assertEqual(self.route("claude", "agy"), "direct")

    def test_claude_host_reaches_codex_through_the_adapter_not_the_companion(self):
        """Both primary hosts must delegate under one contract.

        The official Codex plugin used to own this edge (companion_preferred).
        It was the only path outside this layer, so it carried a different error
        taxonomy, no role_policy, and no doctor coverage -- an asymmetry between
        the two hosts the user actually drives. The plugin stays installed for
        its own commands; it is no longer the routed default.
        """
        self.assertEqual(self.route("claude", "codex"), "direct")

    def test_self_route_is_in_process_for_every_agent_host(self):
        for agent in ("claude", "codex", "grok", "agy"):
            self.assertEqual(self.route(agent, agent), "in_process")

    def test_profile_matrix_tightens_a_direct_base_route(self):
        # codex->agy is direct for review, but work needs a write grant the
        # Codex sandbox cannot create, so the stricter profile entry must win.
        self.assertEqual(self.route("codex", "agy", "review"), "direct")
        self.assertEqual(self.route("codex", "agy", "work"), "broker")

    def test_strictest_route_wins_regardless_of_matrix_order(self):
        config = {
            "host_matrix": {"h": {"p": "direct"}},
            "profile_host_matrix": {"h": {"p": {"review": "broker"}}},
            "mode_host_matrix": {"h": {"p": {"oneshot": "requires_host_escalation"}}},
        }
        self.assertEqual(
            call_worker.route_for(config, "h", "p", "review", "oneshot"),
            "requires_host_escalation",
        )

    def test_unknown_host_falls_back_to_direct(self):
        self.assertEqual(self.route("unregistered", "claude"), "direct")

    def test_every_documented_route_value_is_used_or_reserved(self):
        used = set()
        for row in self.config["host_matrix"].values():
            used.update(row.values())
        for provider_rows in self.config["profile_host_matrix"].values():
            for profiles in provider_rows.values():
                used.update(profiles.values())
        self.assertTrue(used <= set(self.config["routes"]), used - set(self.config["routes"]))


class ModelResolutionTests(unittest.TestCase):
    """A model we cannot read from a real source is reported blank, not guessed."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.config = json.loads((ROOT / "backends.json").read_text(encoding="utf-8"))

    def tearDown(self):
        self.temp.cleanup()

    def test_cli_override_wins(self):
        model, source = call_worker.resolve_model(self.config["providers"]["claude"], "claude-opus-5")
        self.assertEqual(model, "claude-opus-5")
        self.assertEqual(source, "cli_override")

    def test_env_source_is_reported_with_its_variable_name(self):
        provider = {"model_sources": [{"kind": "env", "name": "CROSSCREW_TEST_MODEL"}]}
        os.environ["CROSSCREW_TEST_MODEL"] = "some-model"
        try:
            model, source = call_worker.resolve_model(provider, None)
        finally:
            del os.environ["CROSSCREW_TEST_MODEL"]
        self.assertEqual(model, "some-model")
        self.assertEqual(source, "env:CROSSCREW_TEST_MODEL")

    def test_file_source_is_reported_with_its_path_and_key(self):
        settings = self.base / "settings.json"
        settings.write_text(json.dumps({"model": {"name": "pinned-1"}}), encoding="utf-8")
        provider = {"model_sources": [{"kind": "file", "path": str(settings), "key": "model.name"}]}
        model, source = call_worker.resolve_model(provider, None)
        self.assertEqual(model, "pinned-1")
        self.assertEqual(source, f"file:{settings}:model.name")

    def test_first_resolvable_source_wins(self):
        present = self.base / "present.json"
        present.write_text(json.dumps({"model": "second"}), encoding="utf-8")
        provider = {
            "model_sources": [
                {"kind": "file", "path": str(self.base / "absent.json"), "key": "model"},
                {"kind": "file", "path": str(present), "key": "model"},
            ]
        }
        model, source = call_worker.resolve_model(provider, None)
        self.assertEqual(model, "second")
        self.assertEqual(source, f"file:{present}:model")

    def test_unresolvable_model_is_blank_not_invented(self):
        provider = {"model_sources": [{"kind": "file", "path": str(self.base / "none.json"), "key": "model"}]}
        model, source = call_worker.resolve_model(provider, None)
        self.assertIsNone(model)
        self.assertEqual(source, "account_default_unpinned")

    def test_claude_registry_lists_real_settings_paths(self):
        sources = self.config["providers"]["claude"]["model_sources"]
        kinds = [source["kind"] for source in sources]
        self.assertEqual(kinds[0], "env")
        self.assertEqual(sources[0]["name"], "ANTHROPIC_MODEL")
        paths = [source.get("path") for source in sources if source["kind"] == "file"]
        self.assertIn("~/.claude/settings.json", paths)
        self.assertIn("~/.claude/settings.local.json", paths)


class SelfDelegationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        fake = self.bin / "claude"
        fake.write_text("#!/bin/sh\nprintf 'SELF_OK\\n'\n", encoding="utf-8")
        fake.chmod(0o755)
        self.brief = self.base / "brief.md"
        self.brief.write_text("task", encoding="utf-8")
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin}:{self.env['PATH']}"
        self.env["CROSSCREW_STATE_DIR"] = str(self.base / "state")
        self.env["HOME"] = str(self.base)

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *extra):
        completed = subprocess.run(
            [
                sys.executable,
                str(CALL_WORKER),
                "claude",
                str(self.brief),
                "--host",
                "claude",
                "--target",
                str(self.base),
                *extra,
            ],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=60,
        )
        return completed, json.loads(completed.stdout)

    def test_host_calling_itself_is_refused_by_default(self):
        completed, payload = self.invoke()
        self.assertEqual(completed.returncode, 64)
        self.assertEqual(payload["status"], "self_delegation")
        self.assertEqual(payload["route"], "in_process")
        self.assertNotIn("SELF_OK", json.dumps(payload))

    def test_deliberate_second_opinion_is_still_possible(self):
        completed, payload = self.invoke("--allow-self-delegation")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("SELF_OK", payload["stdout"])

    def test_check_reports_route_and_model_provenance(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(CALL_WORKER),
                "claude",
                "--host",
                "codex",
                "--check",
            ],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=60,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["route"], "broker")
        self.assertIn("broker_available", payload)
        self.assertIn("model_source", payload)
        self.assertIn("model_pinned", payload)
        self.assertFalse(payload["model_pinned"])
        self.assertIsNone(payload["model"])
        self.assertEqual(payload["model_source"], "account_default_unpinned")


if __name__ == "__main__":
    unittest.main()
