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
VERSION = '0.1.0a1'


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h', '--help'):
        print('Crosscrew ' + VERSION + '\n\n'
              'crosscrew job <start|wait|list|status|collect|cancel|mission> ...\n'
              'crosscrew call <provider> ...  (synchronous adapter; --check for routing)\n'
              'crosscrew doctor [--providers claude codex ...]\n'
              'crosscrew broker <serve|health|install|uninstall|restart|status|render>\n'
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
        rows = [{'provider': p, 'cli_available': shutil.which(p) is not None,
                 'billing': billing.inspect(p, Path.cwd())} for p in selected]
        problems = any(not r['cli_available'] or r['billing']['indicators'] for r in rows)
        print(json.dumps({'status': 'attention' if problems else 'checks_passed',
                          'providers': rows, 'state_dir': str(state),
                          'broker_available': broker_client.health(state) is not None,
                          'scope': 'No model calls. CLI presence and known billing overrides only; login, billing and task completion remain unverified.'}, indent=2))
        return 65 if problems else 0
    if command == 'broker':
        import service
        return service.main(args)
    script = {'job': 'worker_job.py', 'call': 'call_worker.py'}.get(command)
    if not script:
        print('Unknown command: ' + command, file=sys.stderr)
        return 64
    os.execv(sys.executable, [sys.executable, str(ROOT / script), *args])


if __name__ == '__main__':
    raise SystemExit(main())
