"""Drain stdout/stderr and feed stdin concurrently; observe stdout without a model."""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time


def run(argv, stdin_text, cwd, env, timeout, observer, stdout_limit, stderr_limit):
    supervised = env.get("CROSSCREW_SUPERVISED") == "1"
    child_env = dict(env)
    child_env.pop("CROSSCREW_PROGRESS_FILE", None)
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=child_env,
                                   start_new_session=not supervised, bufsize=0)
    except OSError as exc:
        return 127, "", f"launch_error: {exc}", False
    output, errors = bytearray(), bytearray()
    pending = (stdin_text or "").encode()
    offset = 0
    started = time.monotonic()
    terminated_at = None
    killed = False
    timed_out = False
    with selectors.DefaultSelector() as selector:
        for stream, channel in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, channel)
        if process.stdin is not None:
            if pending:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()
        try:
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if timeout > 0 and now - started >= timeout and terminated_at is None:
                    timed_out = True
                    terminated_at = now
                    try:
                        process.terminate() if supervised else os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                if terminated_at is not None and now - terminated_at >= 2 and not killed:
                    killed = True
                    try:
                        process.kill() if supervised else os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if killed and now - terminated_at >= 4:
                    break  # inherited pipe FDs must not defeat an explicit timeout
                for key, _ in selector.select(0.05):
                    stream = key.fileobj
                    if key.data == "stdin":
                        try:
                            offset += os.write(stream.fileno(), pending[offset:offset + 32768])
                        except BrokenPipeError:
                            offset = len(pending)
                        except BlockingIOError:
                            continue
                        if offset == len(pending):
                            selector.unregister(stream)
                            stream.close()
                        continue
                    try:
                        chunk = os.read(stream.fileno(), 32768)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    destination = output if key.data == "stdout" else errors
                    limit = stdout_limit if key.data == "stdout" else stderr_limit
                    destination.extend(chunk)
                    if len(destination) > limit:
                        del destination[:len(destination) - limit]
                    if key.data == "stdout":
                        try:
                            observer.feed(chunk)
                        except Exception:
                            observer.degraded = True
            process.wait()
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
    return (124 if timed_out else process.returncode,
            output.decode("utf-8", errors="replace"), errors.decode("utf-8", errors="replace"), timed_out)
