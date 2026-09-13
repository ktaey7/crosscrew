"""Portable public boundaries; synthetic inputs and temporary homes only."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agy_grants
import call_worker
import crosscrew
import grok_sandbox
import usage_summary


class PublicUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='crosscrew-public-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, HOME=str(self.root), GROK_HOME=str(self.root / '.grok'),
                              CROSSCREW_STATE_DIR=str(self.root / 'state'),
                              CROSSCREW_AGY_PROJECTS_DIR=str(self.root / 'projects'))
        self.env.start()
        self.addCleanup(self.env.stop)

    def config(self, paths):
        source = self.root / 'backends.json'
        source.write_text(json.dumps({'providers': {'grok': {'work_sandbox': {'runtime_read_paths': paths}}}}))
        return patch.dict(os.environ, CROSSCREW_BACKENDS_CONFIG=str(source))

    def test_grok_runtime_is_explicit_and_read_only(self):
        runtime = self.root / 'specific runtime'; runtime.mkdir()
        with self.config([str(runtime), str(runtime)]):
            self.assertEqual(grok_sandbox.profile(), {'extends': 'strict', 'read_only': [str(runtime)]})
            grok_sandbox.install()
            self.assertTrue(grok_sandbox.ready())
            self.assertNotIn('read_write', grok_sandbox.path().read_text())
        with self.config([]):
            self.assertFalse(grok_sandbox.ready())
            before = grok_sandbox.path().read_bytes()
            with self.assertRaises(ValueError):
                grok_sandbox.install()
            self.assertEqual(grok_sandbox.path().read_bytes(), before)

    def test_grok_rejects_broad_missing_and_malformed_runtime_paths(self):
        for values in [['/'], [str(self.root)], ['relative'], [str(self.root / 'missing')], ['bad\npath'], 'wrong']:
            with self.subTest(values=values), self.config(values):
                with self.assertRaises(ValueError):
                    grok_sandbox.profile()
        alias = self.root / 'alias'; alias.symlink_to('/')
        with self.config([str(alias)]), self.assertRaises(ValueError):
            grok_sandbox.profile()

    def test_grok_refuses_symlinked_native_directory(self):
        outside = self.root / 'foreign'; outside.mkdir()
        (self.root / '.grok').symlink_to(outside)
        with self.assertRaises(ValueError):
            grok_sandbox.install()
        self.assertEqual(list(outside.iterdir()), [])

    def test_grok_refuses_symlinked_native_file(self):
        source = grok_sandbox.path(); source.parent.mkdir()
        foreign = self.root / 'foreign'; foreign.write_text('keep')
        source.symlink_to(foreign)
        with self.assertRaises(ValueError):
            grok_sandbox.install()
        self.assertEqual(foreign.read_text(), 'keep')

    def test_cleanup_ignores_personal_and_foreign_project_files(self):
        directory = self.root / 'projects'; directory.mkdir()
        foreign = directory / 'multi-ai-work-user.json'; foreign.write_text('keep')
        other = directory / 'unrelated.json'; other.write_text('keep')
        owned = directory / 'crosscrew-work-orphan.json'; owned.write_text('{}')
        agy_grants.cleanup_orphan_projects(directory)
        self.assertFalse(owned.exists())
        self.assertEqual(foreign.read_text(), 'keep')
        self.assertEqual(other.read_text(), 'keep')

    def test_temporary_grant_is_removed_when_sandbox_setup_raises(self):
        brief = self.root / 'brief.md'; brief.write_text('synthetic')
        args = call_worker.parser().parse_args(['agy', str(brief), '--target', str(self.root), '--profile', 'work'])
        args.target = self.root
        args.timeout = 0
        config = call_worker.load_config()
        with patch('call_worker.shutil.which', return_value='/fake/agy'), \
                patch('call_worker.readonly_sandbox.wrap', side_effect=OSError('synthetic setup failure')):
            with self.assertRaises(OSError):
                call_worker.execute_once(args, config, config['providers']['agy'], self.root / 'state', brief, 'fresh', None)
        self.assertEqual(list((self.root / 'projects').glob('crosscrew-work-*')), [])

    def test_doctor_fails_on_missing_broker_without_mutations(self):
        with patch('billing.inspect', return_value={'indicators': []}), \
                patch('crosscrew.shutil.which', return_value='/fake/claude'), \
                patch('broker_client.health', return_value=None), \
                patch('broker_client.unavailable_reason', return_value='no broker endpoint registered'):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(crosscrew.main(['doctor', '--providers', 'claude']), 65)
            value = json.loads(output.getvalue())
            self.assertEqual(value['status'], 'attention')
            self.assertFalse(value['broker_available'])
            self.assertIn('no broker', value['broker_reason'])
            self.assertFalse((self.root / 'state').exists())

    def test_doctor_reports_optional_grok_work_profile_separately(self):
        with patch('billing.inspect', return_value={'indicators': []}), \
                patch('crosscrew.shutil.which', return_value='/fake/grok'), \
                patch('broker_client.health', return_value={'status': 'ok'}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(crosscrew.main(['doctor', '--providers', 'grok']), 0)
            value = json.loads(output.getvalue())
            self.assertFalse(value['providers'][0]['work_profile_ready'])
            self.assertIsNone(value['broker_reason'])
            self.assertFalse((self.root / '.grok').exists())

    def test_usage_rejects_non_envelope_schema(self):
        source = self.root / 'result.json'
        for version in [None, 2, '1.0', '3.0']:
            source.write_text(json.dumps({'schema_version': version, 'provider': 'claude', 'status': 'ok'}))
            self.assertIsNone(usage_summary.load_result(source))
        source.write_text(json.dumps({'schema_version': '2.0', 'provider': 'claude', 'status': 'ok'}))
        report = usage_summary.summarize([source])
        self.assertEqual(report['invalid_files'], 0)
        self.assertIsNone(report['providers']['claude']['tokens']['input_tokens']['reported_sum'])
