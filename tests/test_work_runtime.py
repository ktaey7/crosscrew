import contextlib
import base64
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

import agy_grants
import broker_protocol
import call_worker
import grok_sandbox
import grok_auth
import job_wait
import worker_job
import work_commands


class CommandTests(unittest.TestCase):
    def test_grok_refreshes_expired_cache_without_a_generation_call(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {'GROK_HOME': raw}):
            def cache(expiry):
                body = base64.urlsafe_b64encode(json.dumps({'exp': expiry}).encode()).decode().rstrip('=')
                (Path(raw) / 'auth.json').write_text(json.dumps({'synthetic': {'key': 'header.' + body + '.signature'}}))
            cache(1)
            self.assertTrue(grok_auth.needs_refresh())
            def refresh(*args, **kwargs):
                self.assertEqual(args[0], ['/fake/grok', 'models'])
                cache(9999999999)
                return type('Result', (), {'returncode': 0})()
            with patch('grok_auth.subprocess.run', side_effect=refresh) as run:
                self.assertIsNone(grok_auth.prepare('/fake/grok'))
                self.assertIsNone(grok_auth.prepare('/fake/grok'))
                self.assertEqual(run.call_count, 1)
            cache(1)
            with patch('grok_auth.subprocess.run', return_value=type('Result', (), {'returncode': 1})()):
                self.assertEqual(grok_auth.prepare('/fake/grok'), 'auth_preflight_failed')

    def test_grants_are_literal_anchored_and_not_prefixes(self):
        command = '/example/runtime/bin/python3 -m unittest -q test_smoke'
        for rule in work_commands.permission_rules([command]):
            regex = rule.split('regex:', 1)[1][:-1]
            self.assertIsNotNone(re.fullmatch(regex, command))
            for other in [command + '; echo extra', 'echo prefix; ' + command, command + ' extra']:
                self.assertIsNone(re.fullmatch(regex, other))
        metacharacters = "echo '$HOME (test).* [x] +? {a} | \\end'"
        regex = work_commands.permission_rules([metacharacters])[0].split('regex:', 1)[1][:-1]
        self.assertIsNotNone(re.fullmatch(regex, metacharacters))

    def test_invalid_grants_fail_before_project_creation(self):
        for invalid in ['command(*)', [''], ['echo a\necho b'], ['x' * 4097], [None], ['x'] * 17]:
            with self.assertRaises(ValueError):
                work_commands.validate(invalid, 'agy', 'work')
        for provider, profile in [('grok', 'work'), ('agy', 'review')]:
            with self.assertRaises(ValueError):
                work_commands.validate(['echo ok'], provider, profile)
        self.assertEqual(work_commands.validate(['echo ok', 'echo ok'], 'agy', 'work'), ['echo ok'])

    def test_project_grant_is_temporary_and_target_scoped(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {'CROSSCREW_AGY_PROJECTS_DIR': raw}):
            grant = agy_grants.create_work_project(Path(raw), ['echo ok'])
            try:
                rules = json.loads(grant.project_path.read_text())['permissionGrants']['permissionGrants']['allow']
                self.assertEqual(rules, [f'write_file({raw})', 'command(regex:^echo ok$)', 'unsandboxed(regex:^echo ok$)'])
            finally:
                agy_grants.release_work_project(grant)
            self.assertFalse(grant.project_path.exists())
            self.assertFalse(grant.lock_path.exists())

    def test_broker_supervisor_dispatcher_keep_the_exact_command(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw); brief = target / 'brief.md'; brief.write_text('synthetic')
            command = '/example/runtime/bin/python3 -m unittest -q test_smoke'
            args = worker_job.parser().parse_args(['start', 'agy', str(brief), '--profile', 'work',
                                                  '--host', 'codex', '--target', raw, '--allow-command', command])
            request = broker_protocol.validate(worker_job.broker_start_payload(args, brief, target))
            forwarded = worker_job.parser().parse_args(broker_protocol.build_argv(request)[2:])
            self.assertEqual(forwarded.allow_command, [command])
            dispatcher = call_worker.parser().parse_args(['agy', str(brief), '--profile', 'work',
                                                         '--allow-command', command])
            self.assertEqual(dispatcher.allow_command, [command])
            with self.assertRaises(broker_protocol.RequestError):
                broker_protocol.validate({**request, 'brief': str(brief), 'target': raw, 'allow_commands': ['bad\ncommand']})
            with self.assertRaises(broker_protocol.RequestError):
                broker_protocol.validate({**request, 'action': 'check'})

    def test_command_grants_cannot_launch_without_outer_write_confinement(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw); brief = target / 'brief.md'; brief.write_text('synthetic')
            args = call_worker.parser().parse_args(['agy', str(brief), '--profile', 'work', '--allow-command', 'echo ok'])
            config = call_worker.load_config()
            with patch('call_worker.shutil.which', return_value='/fake/agy'), \
                    patch('call_worker.readonly_sandbox.enabled_for', return_value=False), \
                    patch('call_worker.build_invocation') as build:
                with self.assertRaisesRegex(ValueError, 'outer OS write sandbox'):
                    call_worker.execute_once(args, config, config['providers']['agy'], target, brief, 'fresh', None)
                build.assert_not_called()

    def test_grok_runtime_is_read_only_and_does_not_replace_v1(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {'GROK_HOME': raw}):
            source = Path(raw) / 'sandbox.toml'
            old = '[profiles.existing-user-profile]\nextends="strict"\n'
            source.write_text(old)
            grok_sandbox.install()
            self.assertTrue(source.read_text().startswith(old))
            self.assertTrue(grok_sandbox.ready())
            self.assertNotIn('read_write', grok_sandbox.profile())
            self.assertEqual(grok_sandbox.profile()['read_only'], [])
            self.assertEqual(grok_sandbox.NAME, 'crosscrew-target-v1')


class BatchWaitTests(unittest.TestCase):
    def test_waits_for_both_completions_without_intermediate_output(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for job in ['a', 'b']:
                path = root / 'jobs' / job; path.mkdir(parents=True)
                (path / 'job.json').write_text(json.dumps({'state': 'running', 'pid': os.getpid()}))
            def finish(job):
                (root / 'jobs' / job / 'result.json').write_text('{"status":"ok"}')
            timers = [threading.Timer(delay, finish, [job]) for job, delay in [('a', .06), ('b', .13)]]
            for timer in timers:
                timer.start()
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(job_wait.run_many(root, ['a', 'b'], interval=.05, timeout=2), 0)
                self.assertEqual(len(out.getvalue().splitlines()), 1)
                self.assertTrue(all(row['terminal'] for row in json.loads(out.getvalue())['jobs']))
            finally:
                for timer in timers:
                    timer.join()

    def test_one_output_for_multiple_jobs_and_no_worker_cancel(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for job, status in [('a', 'ok'), ('b', 'auth_expired')]:
                path = root / 'jobs' / job; path.mkdir(parents=True)
                (path / 'job.json').write_text('{"state":"running"}')
                (path / 'result.pending.json').write_text(json.dumps({'status': status, 'stdout': job}))
            before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = job_wait.run_many(root, ['a', 'b', 'a'], summary=True)
            self.assertEqual(code, 1)
            self.assertEqual(len(out.getvalue().splitlines()), 1)
            result = json.loads(out.getvalue())
            self.assertEqual(result['wait_status'], 'finished')
            self.assertEqual([r['result_summary']['stdout'] for r in result['jobs']], ['a', 'b'])
            self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_timeout_keeps_pending_job_and_missing_job_is_not_success(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); path = root / 'jobs' / 'pending'; path.mkdir(parents=True)
            (path / 'job.json').write_text(json.dumps({'state': 'running', 'pid': os.getpid()}))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(job_wait.run_many(root, ['pending'], interval=.05, timeout=.06), 124)
            self.assertEqual(json.loads(out.getvalue())['wait_status'], 'timeout')
            self.assertFalse((path / 'canceled.json').exists())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(job_wait.run_many(root, ['absent']), 66)


if __name__ == '__main__':
    unittest.main()
