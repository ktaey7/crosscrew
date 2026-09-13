#!/usr/bin/env python3
"""Crosscrew: borrow another AI's perspective from your current workspace."""
from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import sys

if sys.version_info < (3, 11):
    raise SystemExit('Crosscrew requires Python 3.11+; rerun with a supported interpreter.')

ROOT = Path(__file__).resolve().parent
VERSION = '0.2.0a1'


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h', '--help'):
        print('Crosscrew ' + VERSION + '\n\n'
              'crosscrew job <start|wait|wait-many|list|status|collect|cancel|mission> ...\n'
              'crosscrew call <provider> ...  (synchronous adapter; --check for routing)\n'
              'crosscrew doctor [--providers claude codex ...]\n'
              'crosscrew broker <serve|health|install|uninstall|restart|status|render>\n'
              'crosscrew grok-sandbox [--render|--install]\n'
              'crosscrew usage RESULT.json ...\n'
              'crosscrew compare MANIFEST.json  (explicit Codex-host comparison)\n'
              'crosscrew card\n'
              'crosscrew --version\n\n'
              'Providers: claude, codex, grok, agy. Native CLIs and login required.\n'
              'See README.md for installation, billing limitations and permissions.')
        return 0
    command = args.pop(0)
    if command == '--version':
        print(VERSION)
        return 0
    if command == 'card':
        import quick_contract
        print(quick_contract.render(), end='')
        return 0
    if command == 'doctor':
        parser = argparse.ArgumentParser()
        parser.add_argument('--providers', nargs='+', choices=['claude','codex','grok','agy'], default=['claude','codex'])
        selected = parser.parse_args(args).providers
        import billing
        import broker_client
        import projection
        config = json.loads((ROOT / 'backends.json').read_text())
        path = os.environ.get('CROSSCREW_BACKENDS_CONFIG')
        if path:
            config = json.loads(Path(path).read_text())
        state = projection.resolve_state_dir(config, ROOT)
        rows = [{'provider': p, 'cli_available': shutil.which(config['providers'][p]['command']) is not None,
                 'billing': billing.inspect(p, Path.cwd())} for p in selected]
        health = broker_client.health(state)
        reason = None if health is not None else (broker_client.unavailable_reason(state) or 'broker health request failed')
        for row in rows:
            if row['provider'] == 'grok':
                import grok_sandbox
                row['work_profile_ready'] = grok_sandbox.ready()
        problems = health is None or any(not r['cli_available'] or r['billing']['indicators'] for r in rows)
        print(json.dumps({'status': 'attention' if problems else 'checks_passed',
                          'providers': rows, 'state_dir': str(state),
                          'broker_available': health is not None, 'broker_reason': reason,
                          'scope': 'Read-only checks: CLI presence, known billing overrides and broker reachability. Login, billing, native work permissions and task completion remain unverified.'}, indent=2))
        return 65 if problems else 0
    if command == 'broker':
        import service
        return service.main(args)
    script = {'job': 'worker_job.py', 'call': 'call_worker.py',
              'grok-sandbox': 'grok_sandbox.py', 'usage': 'usage_summary.py',
              'compare': 'comparison_report.py'}.get(command)
    if not script:
        print('Unknown command: ' + command, file=sys.stderr)
        return 64
    os.execv(sys.executable, [sys.executable, str(ROOT / script), *args])


if __name__ == '__main__':
    raise SystemExit(main())
