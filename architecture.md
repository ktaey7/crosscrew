# Architecture and limits

Crosscrew preserves three existing components:

1. `worker_job.py` supervises persistent jobs, observation and recovery.
2. `call_worker.py` validates requests and builds native provider invocations.
3. `multiai_broker.py` accepts authenticated, typed loopback requests where a host
   cannot launch a provider directly. It rejects arbitrary command arguments.

`crosscrew.py` is the small user-facing dispatcher. `backends.json` describes
providers, profiles, routes and selected capability constraints. The optional
host entrypoint reads a generated calling card; it does not duplicate provider flags.
No AI chooses a transport by guessing an executable command.

## Permissions and trust

| Provider | review/research enforcement | Continuation | Release scope |
|---|---|---|---|
| Claude Code | Prompt guard; native auto permission mode | fresh/resume | Initial review focus |
| Codex | Native read-only sandbox | fresh/resume | Initial review focus |
| Grok | Native sandbox, Claude compat surfaces disabled in worker environment | fresh/resume | Experimental |
| agy | macOS Seatbelt write restrictions plus native mode | No stable fresh session ID | Experimental |

A read-only profile is a requested task scope, not a universal guarantee that no
file can change. Claude hooks and native configuration remain active. Sandboxes
constrain writes differently and are not a guarantee against reading private data
or sending it over the network. Use a suitable target and a minimal brief.

The broker runs with the current user's privileges outside the calling host's
sandbox. Possession of its local token allows the supported operations, including
work-profile writes. The token is kept in a private directory. The broker is not
suitable as a shared remote service or a security boundary against a malicious
process running under the same user account. Do not expose its port remotely.
Its typed contract does not establish that a requesting AI has human approval.

The host is responsible for user authorization, safe task scope and result review.
Crosscrew does not rewrite host instructions, disable all provider hooks, or
share memory/settings across vendors. Grok worker invocation disables Claude
compat import surfaces; an end-to-end hook execution canary remains unverified.
Installing a Grok host skill does not alter Grok's global compat settings.

## Routing

| Host → provider | Claude | Codex | Grok | agy |
|---|---|---|---|---|
| Claude | self | direct | direct | direct |
| Codex | broker | self | broker | direct* |
| Grok | broker | broker | self | broker |
| agy | broker | broker | broker | self |
| ordinary shell | direct | direct | direct | direct |

*Codex → agy work uses the broker. These are configured routes, not a claim that
all combinations have completed real tasks in all host versions. A host without
loopback access cannot use its broker route. Failure is explicit; do not claim a
different host to work around it.

## State, sessions and observation

The default state root is `~/.local/state/crosscrew`. Override it with
`CROSSCREW_STATE_DIR`; the client and service must use the same root. Credentials
stay in provider-managed native authentication stores. Crosscrew stores its own
broker token, prompts, outputs and session metadata locally.

`job start` returns a job ID and spawns a supervised process. Provider session IDs
are recorded when available. A follow-up uses the same run ID, provider and profile
with `--mode resume`; use the same target and include the changed context in the
new brief. Resume depends on native provider session retention. State files cannot
recreate a provider session after it expires. Concurrent requests must not reuse
a session as if they were independent reviewers.

`wait` observes without mutating state. The result reference reports path, byte
size and SHA-256, including pending results. A terminal status without a result is
possible. `status` and `collect` can reconcile state and have write effects.
Wait timeouts and waiter termination do not cancel the worker. Cancellation is an
explicit `job cancel`. Progress events only retain allowlisted metadata; brief,
stdout and stderr files still contain task content and must remain private.

The broker contract hash covers its loaded modules and active registry config.
After updating code/config, explicitly restart an installed service. A stale
broker fails closed. Changing native CLI versions can still break invocation;
a matching hash does not verify provider compatibility.

## Additional inherited capabilities

The engine retains work, research, media artifact handling and optional mission
labels. Media configuration and consumer-account storage requirements are not
part of the first installation path; there is no bundled media skill or automatic
storage setup. Treat those capabilities as experimental. Optional mission labels
can organize work but do not implement Council's discussion semantics.

## Adding a provider

Adding a JSON name alone is insufficient. A new native CLI adapter needs:

- A documented noninteractive input/output contract and native authentication.
- Command allowlist, invocation construction, bounded output/error parsing.
- Honest permission enforcement and validated host routes.
- Session identifiers/resume semantics, or an explicit oneshot-only limit.
- Job lifecycle/progress handling and fake-CLI contract tests.
- Opt-in real execution evidence before claiming supported status.

Meta or another provider's future CLI can be evaluated against this list. No Meta
adapter is included or promised in this release. Keep vendor-specific options in
the adapter, not in host skills, and keep Council rules outside this layer.
