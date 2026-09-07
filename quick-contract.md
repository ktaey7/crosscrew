# Crosscrew calling card

host = the AI environment running this command; provider = the other AI.
Do not pretend to be `shell` to bypass a host route. Do not delegate to yourself.
Use only providers the user has installed and authenticated with their native CLI.
Crosscrew does not guarantee subscription billing; known API overrides are blocked.

Write a brief containing the question, relevant absolute file paths, scope,
completion criteria and verification expectations. Include needed context explicitly;
workers do not automatically receive the host conversation. Minimize private data.

```bash
crosscrew job list --running
crosscrew job start "$PROVIDER" "$BRIEF" --host "$HOST" --target "$TARGET" --profile review --mode fresh --run-id "$RUN_ID"
crosscrew job wait "$JOB_ID"
```

Save the returned job_id. Run wait using the host's supported background shell,
retain its execution handle, and retrieve it when available. Notifications and
resuming the host AI depend on the host. After reconnecting, wait on the same job;
do not start a duplicate. Wait is read-only and returns result_ref.path,
size_bytes and sha256. Verify and read that file, not a guessed filename:
it may be result.pending.json or result.json. A terminal job may have no result;
inspect detail. Worker ok does not replace the host's independent verification.

For another round, write a new brief and start with the same provider, run-id,
profile and target using --mode resume. `fresh` creates continuation state;
`oneshot` does not promise a resumable conversation. agy has no stable fresh mode.
Session expiry may require a new fresh run with an explicit context summary.

Profiles: review (default), research (read-only intent), work (authorized edits).
Claude review uses a prompt guard, not an OS write barrier. Native settings/hooks
can still apply. Work does not authorize writes outside target or external actions.
Media is experimental and requires separate artifact configuration; see architecture.md.
Codex optional --effort <low|medium|high|xhigh|max> is an adapter allowlist, not proof of model support.
Omit it to keep native defaults. Other per-provider flags live in the adapter.

| lifecycle_status | terminal | action |
|---|---|---|
| `starting` | no | Wait for startup evidence. |
| `running` | no | Continue waiting; elapsed time alone is not a stall. |
| `completed` | yes | Read and verify the referenced result. |
| `failed` | yes | Read the failure envelope or detail. |
| `canceled` | yes | Report cancellation. |
| `state_unconfirmed` | no | Observe again; do not assume termination. |

Wait exits: completed 0, failed 1, canceled/interrupted 130, observation error 66,
timeout 124. Read JSON even on nonzero exit. Wait timeout/interruption does NOT
cancel the worker. Explicit cancellation: crosscrew job cancel "$JOB_ID".
collect/status can recover and write state; use wait/list for read-only observation.
needs_attention and strong activity evidence do not establish a stall or success.

needs_host_escalation/broker_stale: report the route problem. Do not restart a
service, change sandbox settings or use --host-escalated without authorization.
auth_expired: native CLI reauthentication; no repeated retries or API fallback.
profile_mismatch/session_stale: fresh run. progress_degraded/event_log_degraded
mean observation trouble, not necessarily worker failure. See architecture.md.
Council discussion rules are optional and maintained separately.
