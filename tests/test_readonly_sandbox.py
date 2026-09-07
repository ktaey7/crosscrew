import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import readonly_sandbox  # noqa: E402


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.config = json.loads((ROOT / "backends.json").read_text(encoding="utf-8"))

    def tearDown(self):
        self.temp.cleanup()

    def test_profile_denies_writes_before_allowing_specific_paths(self):
        text = readonly_sandbox.profile_text(["/some/allowed"])
        self.assertLess(text.index("(deny file-write*)"), text.index("(allow file-write*"))
        self.assertIn('(subpath "/some/allowed")', text)

    def test_broad_temp_directories_are_never_writable(self):
        # A target under /tmp would otherwise become writable and silently
        # defeat the read-only profile.
        self.assertNotIn("/tmp", readonly_sandbox.COMMON_WRITE_PATHS)
        self.assertNotIn("/private/tmp", readonly_sandbox.COMMON_WRITE_PATHS)

    def test_read_only_profiles_exclude_the_target(self):
        target = self.base / "target"
        target.mkdir()
        for profile in ("review", "research"):
            paths = readonly_sandbox.write_paths_for(
                self.config["providers"]["agy"], profile, target, self.base
            )
            self.assertNotIn(str(target), paths, profile)

    def test_work_profile_includes_the_target(self):
        target = self.base / "target"
        target.mkdir()
        paths = readonly_sandbox.write_paths_for(
            self.config["providers"]["agy"], "work", target, self.base
        )
        self.assertIn(str(target), paths)

    def test_sbpl_strings_are_escaped(self):
        text = readonly_sandbox.profile_text(['/weird/pa"th\\x'])
        self.assertIn('\\"', text)
        self.assertIn("\\\\", text)

    def test_agy_declares_os_sandbox_for_every_profile(self):
        sandbox = self.config["providers"]["agy"]["os_sandbox"]
        self.assertTrue(sandbox["enabled"])
        self.assertEqual(set(sandbox["profiles"]), {"review", "work", "research"})
        self.assertIn("~/.gemini", sandbox["state_write_paths"])

    def test_agy_write_policies_name_the_enforcing_mechanism(self):
        policies = self.config["providers"]["agy"]["write_policies"]
        for profile, policy in policies.items():
            self.assertTrue(policy.startswith("os_sandbox"), f"{profile}={policy}")

    def test_providers_without_os_sandbox_are_untouched(self):
        for provider in ("claude", "codex", "grok"):
            config = self.config["providers"][provider]
            self.assertFalse(readonly_sandbox.enabled_for(config, "review"), provider)
            argv, applied = readonly_sandbox.wrap(
                ["echo", "hi"], config, "review", self.base, self.base
            )
            self.assertEqual(argv, ["echo", "hi"])
            self.assertIsNone(applied)

    def test_wrap_prefixes_sandbox_exec_when_enabled(self):
        argv, applied = readonly_sandbox.wrap(
            ["agy", "--print", "x"],
            self.config["providers"]["agy"],
            "review",
            self.base,
            self.base,
        )
        if not readonly_sandbox.supported():
            self.skipTest("sandbox-exec unavailable")
        self.assertEqual(argv[0], "sandbox-exec")
        self.assertEqual(argv[1], "-f")
        self.assertEqual(argv[-3:], ["agy", "--print", "x"])
        self.assertEqual(applied, "os_sandbox_no_write")
        self.assertEqual(Path(argv[2]).stat().st_mode & 0o077, 0)


@unittest.skipUnless(readonly_sandbox.supported(), "sandbox-exec unavailable")
class EnforcementTests(unittest.TestCase):
    """The profile must actually stop writes, not merely describe stopping them."""

    def setUp(self):
        # Deliberately NOT under $TMPDIR: the provider needs temp space writable,
        # so a target living there cannot be confined and would make this test
        # pass for the wrong reason.
        self.base = Path(tempfile.mkdtemp(prefix=".sandbox-test-", dir=ROOT))
        self.target = self.base / "target"
        self.target.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.existing = self.target / "existing.txt"
        self.existing.write_text("original", encoding="utf-8")
        self.config = json.loads((ROOT / "backends.json").read_text(encoding="utf-8"))
        self.agy = self.config["providers"]["agy"]
        # The adapter's scratch directory is separate from the target, as it is
        # in execute_once. Reusing the fixture root here would make the whole
        # fixture writable and hide any leak.
        self.scratch = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.scratch.cleanup()
        shutil.rmtree(self.base, ignore_errors=True)

    def test_fixture_is_outside_every_writable_allowance(self):
        self.assertFalse(
            readonly_sandbox.target_inside_writable_scope(
                self.agy, "review", self.target, Path(tempfile.gettempdir())
            )
        )

    def run_confined(self, profile, script):
        argv, applied = readonly_sandbox.wrap(
            ["/bin/sh", "-c", script],
            self.agy,
            profile,
            self.target,
            Path(self.scratch.name),
        )
        self.assertIsNotNone(applied)
        return subprocess.run(argv, capture_output=True, text=True, timeout=60)

    def test_review_blocks_writes_inside_the_target(self):
        created = self.target / "new.txt"
        self.run_confined("review", f"echo LEAK > {created}")
        self.assertFalse(created.exists())

    def test_review_blocks_overwriting_an_existing_target_file(self):
        self.run_confined("review", f"echo OVERWRITTEN > {self.existing}")
        self.assertEqual(self.existing.read_text(encoding="utf-8").strip(), "original")

    def test_review_blocks_writes_outside_the_target(self):
        created = self.outside / "new.txt"
        self.run_confined("review", f"echo LEAK > {created}")
        self.assertFalse(created.exists())

    def test_work_allows_writes_inside_the_target(self):
        created = self.target / "new.txt"
        self.run_confined("work", f"echo INSIDE > {created}")
        self.assertTrue(created.exists())
        self.assertEqual(created.read_text(encoding="utf-8").strip(), "INSIDE")

    def test_work_still_blocks_writes_outside_the_target(self):
        created = self.outside / "new.txt"
        self.run_confined("work", f"echo LEAK > {created}")
        self.assertFalse(created.exists())

    def test_reads_remain_available_under_confinement(self):
        completed = self.run_confined("review", f"cat {self.existing}")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("original", completed.stdout)


if __name__ == "__main__":
    unittest.main()
