# Host approval and worker permissions

[Usage](usage.md) · [Work runtime](work-runtime.md)

Two decisions remain separate:

1. The host must be allowed to run the particular Crosscrew command and access its
   loopback broker. Approval in one AI application does not approve another host.
2. The worker uses its provider's own permissions plus the selected profile.
   A successful broker connection does not grant every tool inside the worker.

The broker accepts typed operations, never arbitrary client argv. All twelve
cross-provider routes use it; a recursive broker child, unreachable broker or
stale contract stops with an explicit failure. Do not relabel the host as `shell`,
use a direct CLI fallback, install a global allow rule, or silently restart a
service to work around that failure. `--host-escalated` is an explicit diagnostic
operation for an already approved command outside the calling host's sandbox.

## Headless Gemini host

A Gemini/Antigravity host may need native approval to run both the exact start
command and its wait command. Configure that approval through the host's own
interactive flow before a headless invocation. Crosscrew does not modify
`GEMINI.md`, host-global permission rules, MCP settings or future-command policy.
A host-side denial can occur before Crosscrew starts; there may be no job ID.

The worker's `--allow-command` option is different: it supplies a temporary exact
command grant to an agy *work worker*, under OS write confinement. It does not
approve the caller's start/wait command. See [work-runtime.md](work-runtime.md).

## Observe without supervising repeatedly

Run one `job wait JOB_ID --summary` in the host's background shell. Save both the
job ID and the host's execution handle. A host may provide a completion event or
require retrieving that execution session after reconnecting. Crosscrew itself
does not wake a Codex conversation or guarantee notifications in every app.

A wait timeout or interrupted waiter never cancels the worker. Continue waiting
on the same job. For a known batch, `wait-many` takes at most sixteen jobs and emits
one JSON response when they finish or the observation times out. Use an explicit
`job cancel` only when the user wants the worker canceled.

If the broker is missing, start the public broker explicitly in an ordinary
terminal. If stale, finish active work and restart that public instance. If native
authentication expired, use the provider's login flow; do not retry indefinitely or
switch billing methods. No generic approval snippet applies safely to all hosts.
