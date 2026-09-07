#!/usr/bin/env python3
"""Install a launcher and optional named host entrypoints without overwriting files."""
from pathlib import Path
import argparse
import shlex
import sys

if sys.version_info < (3, 11):
    raise SystemExit('Crosscrew requires Python 3.11+; rerun with a supported interpreter.')

ROOT=Path(__file__).resolve().parent
HOSTS=('claude','codex','grok')


def entrypoint(host):
    command=shlex.join([sys.executable,str(ROOT/'crosscrew.py')])
    description='Delegate a scoped review, research or implementation task to another installed AI CLI through Crosscrew.'
    return f'''---
name: crosscrew
description: {description}
---
<!-- crosscrew-install-source: {ROOT} -->
Use this entrypoint when the user wants another AI's perspective or explicitly delegates work.
The current host is `{host}`. Select another installed provider; preserve the user's scope.
Read the compact calling card before starting:

```bash
{command} card
```

Use `{command}` in place of `crosscrew` in the card commands, with `--host {host}`.
The installation is at {ROOT}. The detailed contract is {ROOT / 'architecture.md'}.
Do not run provider CLIs directly or change host settings to work around a failed route.
Council discussion rules are optional and separate from this transport.
'''


def destinations(home,bin_dir,hosts):
    launcher='#!/bin/sh\n# crosscrew-install-source: '+str(ROOT)+'\nexec '+shlex.join([sys.executable,str(ROOT/'crosscrew.py')])+' \"$@\"\n'
    items=[(bin_dir/'crosscrew',launcher)]
    for host in hosts:
        path=(home/'.claude/commands/crosscrew.md' if host=='claude' else home/f'.{host}/skills/crosscrew/SKILL.md')
        items.append((path,entrypoint(host)))
    return items


def owned(path,text):
    if text is None:
        return path.is_symlink() and path.resolve()==(ROOT/'crosscrew').resolve()
    return not path.is_symlink() and path.is_file() and any(line in (f'# crosscrew-install-source: {ROOT}', f'<!-- crosscrew-install-source: {ROOT} -->') for line in path.read_text().splitlines())


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir',type=Path,default=Path.home()/'.local/bin')
    parser.add_argument('--host',action='append',choices=HOSTS,default=[])
    parser.add_argument('--uninstall',action='store_true')
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args(argv)
    items=destinations(Path.home(),args.bin_dir.expanduser().resolve(),dict.fromkeys(args.host))
    # Validate the whole request before touching any destination.
    for path,text in items:
        if path.exists() or path.is_symlink():
            if not owned(path,text):
                parser.error(f'Refusing to overwrite or remove an unowned path: {path}')
    for path,text in items:
        if args.dry_run:
            print(('remove ' if args.uninstall else 'install ')+str(path))
        elif args.uninstall:
            if path.exists() or path.is_symlink(): path.unlink()
        else:
            path.parent.mkdir(parents=True,exist_ok=True)
            if text is None:
                if not path.is_symlink(): path.symlink_to(ROOT/'crosscrew')
            else:
                # Owned generated entrypoints may be refreshed after a source update.
                if path.exists():
                    path.write_text(text)
                else:
                    with path.open('x') as handle: handle.write(text)
                if path.name=='crosscrew': path.chmod(0o755)
    if not args.dry_run:
        print('Done. No native CLI settings or services changed. Runtime state retained.')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
