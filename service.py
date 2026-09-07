"""Optional macOS LaunchAgent; never installed by the ordinary installer."""
from pathlib import Path
import argparse
import os
import plistlib
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
LABEL = 'io.github.ktaey7.crosscrew.broker'


def specification(home=None, env=None):
    home=Path.home() if home is None else Path(home)
    env=os.environ if env is None else env
    logs=home/'Library/Logs/crosscrew'
    child={'PATH':env.get('PATH',os.defpath)}
    for key in ('CROSSCREW_STATE_DIR','CROSSCREW_BACKENDS_CONFIG'):
        if env.get(key):
            child[key]=str(Path(env[key]).expanduser().resolve())
    return {'Label':LABEL, 'ProgramArguments':[sys.executable,str(ROOT/'multiai_broker.py')],
            'WorkingDirectory':str(ROOT), 'RunAtLoad':True,'KeepAlive':True,
            'ProcessType':'Background','StandardOutPath':str(logs/'broker.out.log'),
            'StandardErrorPath':str(logs/'broker.err.log'), 'EnvironmentVariables':child}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['serve','health','install','uninstall','restart','status','render'])
    args=parser.parse_args(argv)
    if args.action in ('serve','health'):
        script='multiai_broker.py' if args.action=='serve' else 'broker_ctl.py'
        extra=[] if args.action=='serve' else ['health']
        return subprocess.call([sys.executable,str(ROOT/script),*extra])
    spec=specification()
    if args.action=='render':
        print(plistlib.dumps(spec).decode(),end='')
        return 0
    if sys.platform!='darwin':
        parser.error('LaunchAgent actions require macOS; use broker serve otherwise (experimental).')
    plist=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
    domain=f'gui/{os.getuid()}'
    service=f'{domain}/{LABEL}'
    if args.action=='status':
        return subprocess.call(['launchctl','print',service])
    # Refuse to manage a registration that points at a different installation.
    if plist.exists():
        existing=plistlib.loads(plist.read_bytes())
        if existing.get('Label')!=LABEL or existing.get('ProgramArguments')!=spec['ProgramArguments']:
            parser.error('Existing Crosscrew service belongs to another installation; uninstall it there first.')
    elif args.action in ('uninstall','restart'):
        parser.error('No service installed by this checkout.')
    if args.action=='install':
        if plist.exists():
            parser.error('Service already installed. Use restart after an update; uninstall/install to change PATH or configuration.')
        plist.parent.mkdir(parents=True,exist_ok=True)
        logs=Path(spec['StandardErrorPath']).parent
        logs.mkdir(parents=True,exist_ok=True,mode=0o700)
        with plist.open('xb') as handle:
            handle.write(plistlib.dumps(spec))
        result=subprocess.call(['launchctl','bootstrap',domain,str(plist)])
        if result:
            print('LaunchAgent was written but bootstrap failed; inspect launchctl status before retrying.',file=sys.stderr)
        return result
    if args.action=='restart':
        return subprocess.call(['launchctl','kickstart','-k',service])
    result=subprocess.run(['launchctl','print',service],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if result.returncode==0:
        stopped=subprocess.call(['launchctl','bootout',service])
        if stopped:
            return stopped
    plist.unlink()
    print('Crosscrew broker removed. Runtime state retained.')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
