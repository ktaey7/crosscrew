"""Real loopback broker with synthetic provider CLIs; no model API calls."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

from tests import test_broker

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ("claude", "codex", "grok", "agy")
FAKE = r'''
import json, os, re, sys, uuid
from pathlib import Path
a = sys.argv[1:]
p = Path(sys.argv[0]).name
def arg(key): return a[a.index(key)+1]
if p in ('claude','codex'): prompt=sys.stdin.read()
elif p=='grok': prompt=Path(arg('--prompt-file')).read_text()
else:
    assert arg('--output-format')=='json'
    prompt=Path(re.search(r'from (.+?) and ',arg('--print')).group(1)).read_text()
resume = '--resume' in a or '--conversation' in a or (p=='codex' and 'resume' in a)
sid = (arg('--resume') if '--resume' in a else arg('--conversation') if '--conversation' in a
       else a[-2] if p=='codex' and resume else arg('--session-id') if '--session-id' in a else str(uuid.uuid4()))
store=Path(os.environ['HOME'])/'.gemini'/'fake-sessions'
store.mkdir(parents=True,exist_ok=True)
path=store/(p+'-'+sid+'.json')
if resume:
    old=json.loads(path.read_text())
    assert old['cwd']==os.getcwd()
    assert 'SYNTHETIC_MARKER=' not in prompt
    answer=old['marker']
else:
    marker=re.search(r'SYNTHETIC_MARKER=(\S+)',prompt).group(1)
    path.write_text(json.dumps(dict(marker=marker,cwd=os.getcwd())))
    answer='ACK'
if p=='claude': print(json.dumps(dict(type='result',subtype='success',is_error=False,result=answer,session_id=sid)))
elif p=='agy': print(json.dumps(dict(status='SUCCESS',response=answer,conversation_id=sid)))
elif p=='codex':
    Path(arg('-o')).write_text(answer)
    print(json.dumps(dict(type='thread.started',thread_id=sid)))
    print(json.dumps({'type':'turn.completed'}))
else: print(answer)
'''


class RouteMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_broker.BrokerServerTests.setUpClass.__func__(cls)
        for provider in PROVIDERS:
            path = cls.bin / provider
            path.write_text(f"#!{sys.executable}\n" + FAKE)
            path.chmod(0o755)

    tearDownClass = classmethod(test_broker.BrokerServerTests.tearDownClass.__func__)

    def run_job(self, *args):
        call = subprocess.run([sys.executable, str(ROOT / "worker_job.py"), *args],
                              env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(call.returncode, 0, call.stdout + call.stderr)
        return json.loads(call.stdout)

    def test_all_twelve_routes_fresh_resume_wait_and_same_bytes_result(self):
        for host in PROVIDERS:
            for provider in PROVIDERS:
                if host == provider:
                    continue
                with self.subTest(host=host, provider=provider):
                    run = f"matrix-{host}-{provider}"
                    sid = None
                    for mode in ("fresh", "resume"):
                        brief = self.base / f"{run}-{mode}.md"
                        brief.write_text(f"SYNTHETIC_MARKER={run}" if mode == "fresh" else "Recall the earlier marker.")
                        start = self.run_job("start", provider, str(brief), "--host", host,
                                             "--mode", mode, "--profile", "review", "--run-id", run,
                                             "--target", str(self.base))
                        self.assertEqual(start["transport"], "broker")
                        end = self.run_job("wait", start["job_id"], "--summary", "--interval", ".05", "--timeout", "20")
                        self.assertTrue(end["terminal"], end)
                        summary = end["result_summary"]
                        self.assertEqual((summary["status"], summary["transport"]), ("ok", "broker"), end)
                        self.assertEqual(summary["stdout"].strip(), "ACK" if mode == "fresh" else run)
                        if mode == "fresh":
                            sid = summary["session_id"]
                            self.assertIsNotNone(sid)
                        else:
                            self.assertEqual(summary["session_id"], sid)
                        ref = end["result_ref"]
                        raw = Path(ref["path"]).read_bytes()
                        self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()), (ref["size_bytes"], ref["sha256"]))
                        result = json.loads(raw)
                        self.assertEqual((result["host"], result["provider"], result["profile"], result["mode"]),
                                         (host, provider, "review", mode))

    def test_broker_children_cannot_recursively_dispatch(self):
        env = dict(self.env, CROSSCREW_BROKER_CHILD="1")
        call = subprocess.run([sys.executable, str(ROOT / "worker_job.py"), "start", "agy", str(self.brief),
                               "--host", "claude", "--target", str(self.base)],
                              env=env, capture_output=True, text=True, timeout=10)
        row = json.loads(call.stdout)
        self.assertEqual(row["status"], "needs_host_escalation")
        self.assertIn("already running as a broker child", row["broker_error"])
