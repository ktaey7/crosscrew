import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import grok_compat
import call_worker


def fixture():
    return {
        "externalCompat": {"cells": [
            {"vendor": "claude", "surface": s, "enabled": False}
            for s in grok_compat.CLAUDE_SURFACES]},
        "hooks": [], "agents": [], "mcpServers": [], "projectInstructions": [],
        "skills": [{"name": s, "source": {"path": f"/test/.agents/skills/{s}/SKILL.md"}}
                   for s in grok_compat.SHARED_SKILLS],
    }


class DiscoveryTests(unittest.TestCase):
    def check(self, data):
        return grok_compat.analyze(data, Path("/test"))

    def test_native_shared_skills_survive_disabled_compat(self):
        self.assertEqual(self.check(fixture())["status"], "healthy")

    def test_each_surface_is_checked_including_rules_and_sessions(self):
        for index in range(len(grok_compat.CLAUDE_SURFACES)):
            data = fixture()
            data["externalCompat"]["cells"][index]["enabled"] = True
            self.assertIn("claude_import_enabled", self.check(data)["problems"])

    def test_missing_duplicate_or_nonboolean_surface_is_unverified(self):
        for kind in ("missing", "duplicate", "string"):
            data = fixture()
            cells = data["externalCompat"]["cells"]
            if kind == "missing":
                cells.pop()
            elif kind == "duplicate":
                cells.append(copy.deepcopy(cells[0]))
            else:
                cells[0]["enabled"] = "false"
            self.assertEqual(self.check(data)["status"], "unverified")

    def test_actual_import_overrides_a_disabled_surface_claim(self):
        data = fixture()
        data["hooks"] = [{"vendor": "claude", "compatibilityStatus": "enabled",
                          "command": "SECRET_SENTINEL"}]
        result = self.check(data)
        self.assertEqual(result["status"], "attention")
        self.assertNotIn("SECRET_SENTINEL", str(result))

    def test_disabled_rows_do_not_count_as_active(self):
        data = fixture()
        data["hooks"] = [{"vendor": "claude", "compatibilityStatus": "disabled"}]
        self.assertEqual(self.check(data)["status"], "healthy")

    def test_shared_skill_from_wrong_source_is_not_accepted(self):
        data = fixture()
        data["skills"][0]["source"]["path"] = "/test/.claude/skills/ai-media/SKILL.md"
        self.assertIn("shared_skill_not_discovered", self.check(data)["problems"])

    def test_unknown_schema_does_not_report_healthy(self):
        for data in ({}, [], {"externalCompat": None}):
            self.assertEqual(self.check(data)["status"], "unverified")

    def test_cli_errors_are_not_leaked(self):
        with patch("grok_compat.subprocess.run", side_effect=OSError("SECRET_SENTINEL")):
            result = grok_compat.inspect()
        self.assertEqual(result["status"], "unverified")
        self.assertNotIn("SECRET_SENTINEL", str(result))

    def test_future_surface_and_malformed_rows_are_unverified(self):
        data = fixture()
        data["externalCompat"]["cells"].append({"vendor": "claude", "surface": "new", "enabled": True})
        self.assertEqual(self.check(data)["status"], "unverified")
        data = fixture()
        data["hooks"] = ["invalid"]
        self.assertEqual(self.check(data)["status"], "unverified")

    def test_worker_overrides_inherited_compat_toggles_in_all_profiles(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            brief = base / 'brief.md'
            brief.write_text('fixture')
            inherited = {f'GROK_CLAUDE_{s.upper()}_ENABLED': 'true' for s in grok_compat.CLAUDE_SURFACES}
            with patch.dict(os.environ, inherited):
                for profile in ('review', 'work', 'research', 'media'):
                    _, _, _, env, _, _ = call_worker.build_invocation(
                        'grok', '/bin/true', 'oneshot', None, brief, base, 0, base, profile, None)
                    for key in inherited:
                        self.assertEqual(env[key], 'false')
