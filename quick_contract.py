"""Generate the compact host-neutral calling card from runtime enums."""
from pathlib import Path
import json
import os
import projection
import effort_registry

ROOT = Path(__file__).resolve().parent
RECOVERY = {
    'starting': 'Wait for startup evidence.',
    'running': 'Continue waiting; elapsed time alone is not a stall.',
    'completed': 'Review the returned evidence and changes.',
    'failed': 'Inspect the failure summary or referenced envelope.',
    'canceled': 'Report cancellation.',
    'state_unconfirmed': 'Observe again; do not assume termination.',
}


def render():
    if set(RECOVERY) != set(projection.LIFECYCLE):
        raise ValueError('Update lifecycle recovery guidance')
    rows = '\n'.join(f'| `{s}` | {"yes" if s in projection.TERMINAL else "no"} | {RECOVERY[s]} |' for s in projection.LIFECYCLE)
    config = json.loads(Path(os.environ.get('CROSSCREW_BACKENDS_CONFIG', ROOT/'backends.json')).read_text())
    efforts = '|'.join(effort_registry.values(config, 'codex'))
    return f'''# Crosscrew calling card

host = your current AI; provider = another installed, authenticated native CLI.
All 12 cross-provider routes use the broker. Self-delegation is refused; answer
in-process. `shell` is for real shell diagnostics, never a host-boundary workaround.
Known API overrides are blocked; subscription coverage is not guaranteed.

Write a brief: goal, absolute target, core references, allowed changes, constraints,
completion criteria and relevant checks. Workers do not receive the host chat.
See docs/worker-workflow.md for brief examples.

```bash
crosscrew job list --running
crosscrew job start "$PROVIDER" "$BRIEF" --host "$HOST" --target "$TARGET" --profile review --mode fresh --run-id "$RUN_ID"
crosscrew job wait "$JOB_ID" --summary
crosscrew job wait-many JOB1 JOB2 --summary
```

Start exit 0 acknowledges acceptance (running + job_id), not completion. Save IDs.
Use the host's background shell and keep its handle. Notification/resumption
varies by host; Crosscrew does not wake a Codex conversation. Avoid repeated
short waits or another supervisor AI.
After reconnecting, wait on the same job; do not start duplicates.

Wait is read-only. --summary returns result_ref path/size/SHA-256 and the answer
(up to 8 KiB), session and usage from those same bytes. If stdout_truncated is
false, do not reread/rehash merely to collect the same answer. Still verify its
claims, sources, diff and tests. If truncated, read result_ref for the rest.
Do not assume result.json: result.pending.json is valid too. Default wait hides
the body. wait-many handles at most 16 jobs in one process with one JSON output
on terminal/timeout. A terminal job can lack a result; inspect detail.

All four providers support oneshot/fresh/resume. Use fresh when follow-up is
likely, then resume with the same provider/run-id/target/profile. A new scope
needs fresh. Session IDs are provider-specific; do not share them. Missing or
mismatched Gemini IDs fail closed. Older records without target lack that check.

Profiles: review (default), research (read-only intent), work (authorized edits).
Work does not authorize external actions, installs, credentials, commit/push or
writes outside target. Host command approval and worker permissions are separate.
Gemini headless hosts need specific start/wait command approval; no global grants
are installed. See docs/host-approvals.md.
Grok work requires an explicit strict custom profile; see docs/work-runtime.md.
Gemini work may use repeated --allow-command '<exact command>' only under outer
OS write confinement. Grants are temporary, not wildcard/prefix authorization.
Read and write boundaries differ by provider; see architecture.md.
Codex optional --effort <{efforts}> is the adapter allowlist, not a model guarantee.
Omit model/effort to retain native defaults. Media remains experimental.

| lifecycle_status | terminal | action |
|---|---|---|
{rows}

Wait exits: completed 0, failed 1, canceled/interrupted 130, observation error 66,
timeout 124. Read JSON on nonzero exit. Wait timeout/interruption never cancels
workers. collect/status can recover and write state. needs_attention/activity
signals establish neither failure nor success. Cancel only when requested.

needs_host_escalation/broker_stale: diagnose; no automatic restart/direct fallback.
auth_expired: native reauthentication, no retry loop or API fallback.
profile_mismatch/target_mismatch/session_stale: fresh. tool_permission_denied:
review the exact native denial; do not broaden permissions automatically.
progress_degraded/event_log_degraded are observation issues. Worker ok is not
host acceptance. Council rules are separate.
'''


if __name__ == '__main__':
    print(render(), end='')
