import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from grok_video_output_setup import (
    BEGIN_MARKER,
    END_MARKER,
    _atomic_private_write,
    _render_block,
    _replace_managed_block,
    _validate_public_args,
)


class GrokVideoOutputSetupTests(unittest.TestCase):
    def test_public_args_require_https_origin_and_safe_prefix(self):
        _validate_public_args(
            "private-videos",
            "https://account.r2.cloudflarestorage.com",
            "auto",
            "grok-videos/",
        )
        for endpoint in ("http://localhost:9000", "https://example.com/path"):
            with self.assertRaises(ValueError):
                _validate_public_args("private-videos", endpoint, "auto", "grok-videos/")
        with self.assertRaises(ValueError):
            _validate_public_args("private-videos", "https://example.com", "auto", "../videos/")

    def test_managed_block_replaces_only_itself(self):
        first = _render_block(
            bucket="private-videos",
            endpoint="https://one.example.com",
            region="auto",
            key_prefix="grok-videos/",
            access_key_id="one",
            secret_access_key="secret-one",
        )
        second = _render_block(
            bucket="private-videos",
            endpoint="https://two.example.com",
            region="auto",
            key_prefix="grok-videos/",
            access_key_id="two",
            secret_access_key="secret-two",
        )
        updated = _replace_managed_block("[ui]\ntheme = \"dark\"\n\n" + first, second)
        self.assertIn("[ui]", updated)
        self.assertIn("https://two.example.com", updated)
        self.assertNotIn("https://one.example.com", updated)
        self.assertEqual(updated.count(BEGIN_MARKER), 1)
        self.assertEqual(updated.count(END_MARKER), 1)

    def test_unmanaged_tools_table_is_not_overwritten(self):
        with self.assertRaises(ValueError):
            _replace_managed_block("[tools]\nrespect_gitignore = true\n", "managed")

    def test_atomic_write_and_backup_are_mode_0600(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / ".grok" / "config.toml"
            self.assertIsNone(_atomic_private_write(path, "first\n"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            backup = _atomic_private_write(path, "second\n")
            self.assertIsNotNone(backup)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(backup.read_text(), "first\n")
            self.assertEqual(path.read_text(), "second\n")


if __name__ == "__main__":
    unittest.main()
