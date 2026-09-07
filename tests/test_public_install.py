from pathlib import Path
import hashlib
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import billing
import broker_protocol
import install
import projection
import quick_contract
import service

ROOT=Path(__file__).resolve().parents[1]


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='crosscrew install ')
        self.home=Path(self.temp.name)
        self.bin=self.home/'bin with spaces'
        self.env=dict(os.environ, HOME=str(self.home))

    def tearDown(self):
        self.temp.cleanup()

    def run_install(self,*args):
        return subprocess.run([sys.executable,str(ROOT/'install.py'),'--bin-dir',str(self.bin),*args],env=self.env,text=True,capture_output=True)

    def test_install_and_remove_only_selected_entrypoints(self):
        original=self.home/'.claude/commands/council.md'
        original.parent.mkdir(parents=True)
        original.write_text('user council')
        self.assertEqual(self.run_install('--host','claude','--host','codex').returncode,0)
        launcher=self.bin/'crosscrew'
        run=subprocess.run([str(launcher),'--version'],env=self.env,text=True,capture_output=True)
        self.assertEqual((run.returncode,run.stdout.strip()),(0,'0.1.0a3'))
        self.assertTrue((self.home/'.codex/skills/crosscrew/SKILL.md').exists())
        self.assertFalse((self.home/'.grok').exists())
        state=self.home/'.local/state/crosscrew/jobs';state.mkdir(parents=True)
        self.assertEqual(self.run_install('--uninstall','--host','claude','--host','codex').returncode,0)
        self.assertFalse(launcher.exists())
        self.assertFalse((self.home/'.claude/commands/crosscrew.md').exists())
        self.assertEqual(original.read_text(),'user council')
        self.assertTrue(state.exists())

    def test_preflight_conflict_leaves_all_destinations_untouched(self):
        target=self.home/'.codex/skills/crosscrew/SKILL.md'
        target.parent.mkdir(parents=True);target.write_text('existing user skill')
        result=self.run_install('--host','claude','--host','codex')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(target.read_text(),'existing user skill')
        self.assertFalse(self.bin.exists())
        self.assertFalse((self.home/'.claude').exists())

    def test_dry_run_creates_nothing(self):
        self.assertEqual(self.run_install('--dry-run','--host','grok').returncode,0)
        self.assertEqual(list(self.home.iterdir()),[])

    def test_symlink_to_foreign_file_is_not_followed(self):
        foreign=self.home/'foreign';foreign.write_text('keep')
        self.bin.mkdir();(self.bin/'crosscrew').symlink_to(foreign)
        self.assertNotEqual(self.run_install().returncode,0)
        self.assertEqual(foreign.read_text(),'keep')

    def test_installed_card_uses_selected_interpreter_and_host(self):
        self.assertEqual(self.run_install('--host','codex').returncode,0)
        card=(self.home/'.codex/skills/crosscrew/SKILL.md').read_text()
        self.assertIn('--host codex',card)
        self.assertIn(sys.executable,card)


class BillingTests(unittest.TestCase):
    def test_known_override_reports_name_not_secret(self):
        with tempfile.TemporaryDirectory() as home:
            report=billing.inspect('grok',Path(home),env={'XAI_API_KEY':'never-print-this'},home=home)
        self.assertEqual(report['status'],'blocked_known_override')
        self.assertNotIn('never-print-this',json.dumps(report))

    def test_absence_of_override_does_not_claim_subscription_verified(self):
        with tempfile.TemporaryDirectory() as home:
            report=billing.inspect('claude',Path(home),env={},home=home)
        self.assertEqual(report['status'],'native_cli_auth_unverified')

    def test_project_api_key_helper_is_blocked_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);target=root/'project';cfg=target/'.claude/settings.json'
            cfg.parent.mkdir(parents=True);cfg.write_text(json.dumps({'apiKeyHelper':'touch must-not-exist'}))
            report=billing.inspect('claude',target,env={},home=root/'home')
            self.assertIn('claude:apiKeyHelper',report['indicators'])
            self.assertFalse((target/'must-not-exist').exists())

    def test_api_config_blocks_start_before_creating_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);brief=root/'brief.md';brief.write_text('test')
            env=dict(os.environ,HOME=directory,CROSSCREW_STATE_DIR=str(root/'state'),ANTHROPIC_API_KEY='not-a-real-secret')
            result=subprocess.run([sys.executable,str(ROOT/'crosscrew.py'),'job','start','claude',str(brief),'--target',directory],env=env,text=True,capture_output=True)
            self.assertEqual(result.returncode,78,result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'],'billing_configuration_blocked')
            self.assertFalse(list((root/'state/jobs').glob('*')))
            self.assertNotIn('not-a-real-secret',result.stdout)


class PublicContractTests(unittest.TestCase):
    def test_plist_preserves_paths_and_does_not_serialize_api_keys(self):
        spec=service.specification(Path('/tmp/home & spaces'),{'PATH':'/a & b/bin','ANTHROPIC_API_KEY':'secret','CROSSCREW_STATE_DIR':'/tmp/state & path'})
        restored=plistlib.loads(plistlib.dumps(spec))
        self.assertEqual(restored,spec)
        self.assertNotIn('secret',repr(spec))
        self.assertEqual(spec['EnvironmentVariables']['CROSSCREW_STATE_DIR'],str(Path('/tmp/state & path').resolve()))
        self.assertEqual(spec['Label'],'io.github.ktaey7.crosscrew.broker')

    def test_override_config_participates_in_broker_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json';path.write_text('{}')
            with patch.object(broker_protocol,'CONFIG_PATH',path):
                first=broker_protocol.contract_hash();path.write_text('{"changed":true}')
                self.assertNotEqual(first,broker_protocol.contract_hash())

    def test_card_stays_small_and_current(self):
        text=quick_contract.render()
        self.assertEqual(text,(ROOT/'quick-contract.md').read_text())
        self.assertLess(len(text.encode()),4096)
        with patch.object(projection,'LIFECYCLE',(*projection.LIFECYCLE,'unknown')):
            with self.assertRaises(ValueError):quick_contract.render()

    def test_legacy_environment_does_not_select_personal_state(self):
        config=json.loads((ROOT/'backends.json').read_text())
        with patch.dict(os.environ,{'HOME':'/tmp/crosscrew-home','MULTIAI_STATE_DIR':'/private/personal-state'},clear=True):
            state=projection.resolve_state_dir(config,ROOT)
        self.assertEqual(str(state),'/tmp/crosscrew-home/.local/state/crosscrew')


class BrokerStartupTests(unittest.TestCase):
    def test_loopback_bind_does_not_consult_dns(self):
        from multiai_broker import LoopbackServer, Handler
        with patch('socket.getfqdn', side_effect=AssertionError('unexpected DNS lookup')):
            with LoopbackServer(('127.0.0.1', 0), Handler) as server:
                self.assertEqual(server.server_name, '127.0.0.1')
                self.assertGreater(server.server_port, 0)
