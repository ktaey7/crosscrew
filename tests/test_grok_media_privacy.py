import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from grok_media_privacy import private_video_output, privacy_scope, video_privacy_preflight


class GrokMediaPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / ".grok"
        self.home.mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def write_auth(self, **updates):
        entry = {
            "auth_mode": "oidc",
            "principal_type": "User",
            "team_blocked_reasons": [],
            "coding_data_retention_opt_out": False,
        }
        entry.update(updates)
        (self.home / "auth.json").write_text(
            json.dumps({"scope": entry}),
            encoding="utf-8",
        )

    def write_config(self, *, mode=0o600, endpoint="https://example.r2.cloudflarestorage.com"):
        path = self.home / "config.toml"
        path.write_text(
            f"""
[tools]
disable_zdr_incompatible_tools = true

[tools.zdr_video_output_s3]
bucket = "grok-private-video"
endpoint = "{endpoint}"
region = "auto"
key_prefix = "grok-videos/"

[tools.zdr_video_output_s3.read_write]
access_key_id = "access"
secret_access_key = "secret"
""".lstrip(),
            encoding="utf-8",
        )
        path.chmod(mode)
        return path

    def test_personal_privacy_opt_out_is_not_team_zdr_but_needs_private_output(self):
        self.write_auth(coding_data_retention_opt_out=True)
        self.assertEqual(privacy_scope(self.home), "coding_data_opt_out")
        report = video_privacy_preflight(self.home)
        self.assertTrue(report["private_output_required"])
        self.assertFalse(report["ready"])

    def test_team_zdr_is_reported_separately(self):
        self.write_auth(team_blocked_reasons=["BLOCKED_REASON_NO_LOGS"])
        self.assertEqual(privacy_scope(self.home), "team_zdr")

    def test_private_complete_https_config_is_ready(self):
        self.write_auth(coding_data_retention_opt_out=True)
        self.write_config()
        report = video_privacy_preflight(self.home)
        self.assertTrue(report["private_output_configured"])
        self.assertTrue(report["ready"])

    def test_secret_bearing_config_must_be_mode_0600(self):
        self.write_config(mode=0o644)
        report = private_video_output(self.home)
        self.assertTrue(report["complete"])
        self.assertFalse(report["private_permissions"])
        self.assertFalse(report["configured"])

    def test_non_https_endpoint_is_rejected(self):
        self.write_config(endpoint="http://localhost:9000")
        self.assertFalse(private_video_output(self.home)["complete"])


if __name__ == "__main__":
    unittest.main()


class SchemaDriftTests(unittest.TestCase):
    """A renamed xAI field must not read as "no restriction"."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / ".grok"
        self.home.mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def write_raw(self, entry):
        (self.home / "auth.json").write_text(json.dumps({"scope": entry}), encoding="utf-8")

    def test_recognized_unrestricted_account_needs_no_private_output(self):
        self.write_raw({"team_blocked_reasons": [], "coding_data_retention_opt_out": False})
        report = video_privacy_preflight(self.home)
        self.assertEqual(report["privacy_scope"], "standard")
        self.assertTrue(report["schema_recognized"])
        self.assertFalse(report["private_output_required"])
        self.assertTrue(report["ready"])

    def test_renamed_fields_fail_closed_instead_of_reading_as_standard(self):
        # xAI renames the retention flag: the old keys disappear entirely.
        self.write_raw({"auth_mode": "oidc", "data_retention_preference": "opt_out"})
        report = video_privacy_preflight(self.home)
        self.assertEqual(report["privacy_scope"], "schema_unrecognized")
        self.assertFalse(report["schema_recognized"])
        self.assertTrue(report["private_output_required"])
        self.assertFalse(report["ready"])
        self.assertTrue(report["fail_closed"])
        self.assertIn("스키마 변경", report["schema_drift"])

    def test_unreadable_auth_record_also_fails_closed(self):
        (self.home / "auth.json").write_text("not json", encoding="utf-8")
        report = video_privacy_preflight(self.home)
        self.assertEqual(report["privacy_scope"], "unknown")
        self.assertTrue(report["private_output_required"])
        self.assertFalse(report["ready"])

    def test_drift_is_absent_from_a_healthy_report(self):
        self.write_raw({"coding_data_retention_opt_out": True})
        report = video_privacy_preflight(self.home)
        self.assertNotIn("schema_drift", report)
        self.assertNotIn("fail_closed", report)

    def test_present_but_false_flag_still_counts_as_recognized(self):
        # Presence of the key is the schema signal, not its value.
        self.write_raw({"coding_data_retention_opt_out": False})
        self.assertTrue(video_privacy_preflight(self.home)["schema_recognized"])
