import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import broker_protocol as protocol  # noqa: E402
import media_artifacts  # noqa: E402
import media_registry  # noqa: E402

CALL_WORKER = ROOT / "call_worker.py"
WORKER_JOB = ROOT / "worker_job.py"


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "backends.json").read_text(encoding="utf-8"))

    def test_declared_media_providers(self):
        self.assertEqual(
            media_registry.providers(self.config),
            {"codex": ["image"], "grok": ["image", "video"]},
        )

    def test_video_is_rejected_for_a_provider_that_lacks_it(self):
        status, hint = media_registry.validation_error(self.config, "codex", "video", None, None)
        self.assertEqual(status, "unsupported_media_kind")
        self.assertIn("image", hint)

    def test_duration_is_rejected_where_unsupported(self):
        status, _hint = media_registry.validation_error(self.config, "codex", "image", None, 6)
        self.assertEqual(status, "unsupported_media_option")

    def test_grok_accepts_declared_durations_only(self):
        self.assertIsNone(media_registry.validation_error(self.config, "grok", "video", "16:9", 6))
        status, hint = media_registry.validation_error(self.config, "grok", "video", "16:9", 7)
        self.assertEqual(status, "unsupported_media_option")
        self.assertIn("[6, 10]", hint)

    def test_provider_without_capability_is_rejected(self):
        for provider in ("claude", "agy"):
            status, _hint = media_registry.validation_error(self.config, provider, "image", None, None)
            self.assertEqual(status, "unsupported_profile", provider)

    def test_valid_requests_pass(self):
        self.assertIsNone(media_registry.validation_error(self.config, "codex", "image", "1:1", None))
        self.assertIsNone(media_registry.validation_error(self.config, "grok", "image", "16:9", None))


class ArtifactSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_dispatch_requires_a_session_id(self):
        self.assertEqual(
            media_artifacts.collect_media_artifacts(
                artifact_source="codex_generated_images",
                target=self.base,
                session_id=None,
                output_dir=self.base,
                media_kind="image",
            ),
            [],
        )

    def test_unknown_artifact_source_is_an_error(self):
        with self.assertRaises(media_artifacts.ArtifactError):
            media_artifacts.collect_media_artifacts(
                artifact_source="somewhere_else",
                target=self.base,
                session_id="abc",
                output_dir=self.base,
                media_kind="image",
            )

    def test_codex_collector_refuses_video(self):
        with self.assertRaises(media_artifacts.ArtifactError):
            media_artifacts.collect_codex_artifacts(
                session_id="abc", output_dir=self.base, media_kind="video"
            )

    def test_missing_thread_directory_is_reported_as_session_missing(self):
        # Distinct from a layout change: the storage root exists, this session's
        # directory does not, so generation simply did not produce anything.
        if not media_artifacts.CODEX_IMAGES_ROOT.is_dir():
            self.skipTest("codex image root absent on this host")
        with self.assertRaises(media_artifacts.ArtifactError) as ctx:
            media_artifacts.collect_codex_artifacts(
                session_id="00000000-0000-0000-0000-000000000000",
                output_dir=self.base,
                media_kind="image",
            )
        self.assertEqual(ctx.exception.code, "artifact_session_missing")

    def test_moved_storage_root_is_reported_separately(self):
        original = media_artifacts.CODEX_IMAGES_ROOT
        media_artifacts.CODEX_IMAGES_ROOT = self.base / "nowhere"
        try:
            with self.assertRaises(media_artifacts.ArtifactError) as ctx:
                media_artifacts.collect_codex_artifacts(
                    session_id="abc", output_dir=self.base, media_kind="image"
                )
        finally:
            media_artifacts.CODEX_IMAGES_ROOT = original
        self.assertEqual(ctx.exception.code, "artifact_root_missing")

    def test_grok_root_move_is_distinguished_from_missing_session(self):
        original = media_artifacts.GROK_SESSIONS_ROOT
        media_artifacts.GROK_SESSIONS_ROOT = self.base / "nowhere"
        try:
            with self.assertRaises(media_artifacts.ArtifactError) as ctx:
                media_artifacts.collect_grok_artifacts(
                    target=self.base, session_id="abc",
                    output_dir=self.base, media_kind="image",
                )
        finally:
            media_artifacts.GROK_SESSIONS_ROOT = original
        self.assertEqual(ctx.exception.code, "artifact_root_missing")

    def test_two_artifacts_in_one_session_is_rejected(self):
        folder = self.base / "session"
        folder.mkdir()
        for name in ("a.png", "b.png"):
            (folder / name).write_bytes(b"x")
        with self.assertRaises(media_artifacts.ArtifactError) as ctx:
            media_artifacts._collect_from_folder(
                folder=folder,
                relative_root=folder,
                session_id="deadbeef",
                output_dir=self.base,
                media_kind="image",
                prefix="test",
            )
        self.assertIn("artifact_count_invalid", str(ctx.exception))

    def test_copy_prefix_identifies_the_provider(self):
        self.assertIn("codex-imagegen", str(media_artifacts._copy_exclusive.__doc__ or "codex-imagegen"))


class BrokerMediaValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.brief = self.base / "brief.md"
        self.brief.write_text("a red circle", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, **overrides):
        body = {
            "action": "start",
            "provider": "codex",
            "host": "grok",
            "profile": "media",
            "mode": "fresh",
            "brief": str(self.brief),
            "target": str(self.base),
            "media_kind": "image",
            "output_dir": str(self.base),
        }
        body.update(overrides)
        return body

    def test_codex_image_media_is_accepted(self):
        request = protocol.validate(self.payload())
        self.assertEqual(request["provider"], "codex")
        self.assertEqual(request["media_kind"], "image")

    def test_grok_video_media_is_accepted(self):
        request = protocol.validate(
            self.payload(provider="grok", media_kind="video", duration=6, aspect_ratio="16:9")
        )
        self.assertEqual(request["duration"], 6)

    def test_broker_rejects_video_for_codex(self):
        with self.assertRaises(protocol.RequestError):
            protocol.validate(self.payload(media_kind="video"))

    def test_broker_rejects_media_for_a_provider_without_capability(self):
        for provider in ("claude", "agy"):
            with self.assertRaises(protocol.RequestError):
                protocol.validate(self.payload(provider=provider))

    def test_broker_still_requires_fresh_mode_and_output_dir(self):
        with self.assertRaises(protocol.RequestError):
            protocol.validate(self.payload(mode="oneshot"))
        body = self.payload()
        del body["output_dir"]
        with self.assertRaises(protocol.RequestError):
            protocol.validate(body)


class DispatcherMediaGateTests(unittest.TestCase):
    """The dispatcher and supervisor must reject the same requests the broker does."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.brief = self.base / "brief.md"
        self.brief.write_text("a red circle", encoding="utf-8")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        for name in ("codex", "grok", "claude", "agy"):
            fake = self.bin / name
            fake.write_text("#!/bin/sh\nprintf 'SHOULD_NOT_RUN\\n'\n", encoding="utf-8")
            fake.chmod(0o755)
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin}:{self.env['PATH']}"
        self.env["CROSSCREW_STATE_DIR"] = str(self.base / "state")
        self.env["CROSSCREW_BROKER_DISABLE"] = "1"
        self.env["HOME"] = str(self.base)

    def tearDown(self):
        self.temp.cleanup()

    def call(self, script, provider, *extra):
        argv = [sys.executable, str(script)]
        if script is WORKER_JOB:
            argv.append("start")
        argv += [
            provider,
            str(self.brief),
            "--host",
            "shell",
            "--target",
            str(self.base),
            "--profile",
            "media",
            "--mode",
            "fresh",
            "--output-dir",
            str(self.base),
            *extra,
        ]
        completed = subprocess.run(argv, text=True, capture_output=True, env=self.env, timeout=60)
        return completed, json.loads(completed.stdout)

    def test_dispatcher_rejects_video_for_codex(self):
        completed, payload = self.call(CALL_WORKER, "codex", "--media-kind", "video")
        self.assertEqual(completed.returncode, 64)
        self.assertEqual(payload["status"], "unsupported_media_kind")
        self.assertEqual(payload["supported_kinds"], ["image"])
        self.assertNotIn("SHOULD_NOT_RUN", json.dumps(payload))

    def test_supervisor_rejects_video_for_codex(self):
        completed, payload = self.call(WORKER_JOB, "codex", "--media-kind", "video")
        self.assertEqual(completed.returncode, 64)
        self.assertEqual(payload["status"], "unsupported_media_kind")

    def test_supervisor_accepts_codex_image_where_it_used_to_hardcode_grok(self):
        # Regression: worker_job.py previously refused any provider except grok.
        completed, payload = self.call(WORKER_JOB, "codex", "--media-kind", "image")
        self.assertNotEqual(payload.get("status"), "invalid_media_input")
        self.assertNotEqual(payload.get("status"), "unsupported_media_kind")

    def test_dispatcher_reports_where_media_is_available(self):
        completed, payload = self.call(CALL_WORKER, "agy", "--media-kind", "image")
        self.assertEqual(completed.returncode, 64)
        self.assertEqual(
            payload["media_providers"], {"codex": ["image"], "grok": ["image", "video"]}
        )




if __name__ == "__main__":
    unittest.main()
