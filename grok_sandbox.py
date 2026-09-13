"""Explicit, additive Grok work profile; runtime directories are opt-in.

No host-specific Python/Homebrew paths are inferred or granted. Configure
providers.grok.work_sandbox.runtime_read_paths in CROSSCREW_BACKENDS_CONFIG,
review the rendered stanza, then explicitly install. Existing profiles survive.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tomllib

NAME = 'crosscrew-target-v1'
ROOT = Path(__file__).resolve().parent


def profile() -> dict:
    config_path = Path(os.environ.get('CROSSCREW_BACKENDS_CONFIG', ROOT / 'backends.json'))
    config = json.loads(config_path.read_text())
    spec = config.get('providers', {}).get('grok', {}).get('work_sandbox', {})
    if spec.get('profile', NAME) != NAME:
        raise ValueError('Unsupported work profile name; keep the public namespace')
    paths = spec.get('runtime_read_paths', [])
    if not isinstance(paths, list) or len(paths) > 32:
        raise ValueError('runtime_read_paths must contain at most 32 explicit directories')
    broad = {'/', '/Users', '/home', '/root', '/etc', '/opt', '/usr', '/usr/local', '/opt/homebrew', str(Path.home())}
    checked = []
    for value in paths:
        if not isinstance(value, str) or any(ord(c) < 32 for c in value):
            raise ValueError('Invalid runtime directory')
        path = Path(value)
        if not path.is_absolute() or not path.is_dir() or str(path.resolve()) in broad or str(path) in broad:
            raise ValueError('Runtime grants require specific existing absolute directories, not broad roots')
        if value not in checked:
            checked.append(value)
    return {'extends': 'strict', 'read_only': checked}


def stanza() -> str:
    spec = profile()
    return ('\n# Crosscrew: opt-in strict target work profile.\n'
            f'[profiles.{NAME}]\nextends = "strict"\nread_only = '
            + json.dumps(spec['read_only']) + '\n')


def path() -> Path:
    return Path(os.environ.get('GROK_HOME', str(Path.home() / '.grok'))) / 'sandbox.toml'


def check_path(source: Path) -> None:
    # macOS exposes temporary directories through these system aliases.
    system_aliases = {'/var': '/private/var', '/tmp': '/private/tmp'} if sys.platform == 'darwin' else {}
    for part in (source, *source.parents):
        if part.is_symlink() and system_aliases.get(str(part)) != str(part.resolve()):
            raise ValueError('Refusing symlinked Grok configuration')
    if source.exists() and (not source.is_file() or source.stat().st_size > 65536):
        raise ValueError('Invalid Grok sandbox file')


def ready() -> bool:
    try:
        source = path()
        check_path(source)
        return tomllib.loads(source.read_text()).get('profiles', {}).get(NAME) == profile()
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def install() -> None:
    """Only this explicit operation writes a native profile; never overwrite one."""
    source = path()
    check_path(source)
    raw = source.read_text() if source.exists() else ''
    config = tomllib.loads(raw)
    expected = profile()
    previous = config.get('profiles', {}).get(NAME)
    if previous is not None:
        if previous != expected:
            raise ValueError('Named profile differs; preserve/review it manually before migration')
        return
    source.parent.mkdir(parents=True, exist_ok=True)
    with source.open('a') as stream:
        stream.write(stanza())
    source.chmod(0o600)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--install', action='store_true')
    group.add_argument('--render', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.render:
            print(stanza(), end='')
            return 0
        if args.install:
            install()
        available = ready()
        print('ready' if available else 'not_ready')
        return 0 if available else 78
    except (OSError, ValueError, TypeError, AttributeError) as error:
        print(str(error), file=sys.stderr)
        return 78


if __name__ == '__main__':
    raise SystemExit(main())
