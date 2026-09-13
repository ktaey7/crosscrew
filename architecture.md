# Architecture and limits

Crosscrew preserves three existing components:

1. `worker_job.py` supervises persistent jobs, observation and recovery.
2. `call_worker.py` validates requests and builds native provider invocations.
3. `multiai_broker.py` accepts authenticated, typed loopback requests from all
   normal AI hosts. It rejects arbitrary command arguments.

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
| agy | macOS Seatbelt write restrictions plus native mode | Native JSON conversation ID; fresh/resume | Experimental |

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
| Claude | self | broker | broker | broker |
| Codex | broker | self | broker | broker |
| Grok | broker | broker | self | broker |
| agy | broker | broker | broker | self |
| ordinary shell | direct | direct | direct | direct |

The ordinary-shell route is for diagnostics. These are configured routes, not a claim that
all combinations have completed real tasks in all host versions. A host without
loopback access cannot use its broker route. Failure is explicit; do not claim a
different host to work around it. Recursive broker children cannot escalate again.
No automatic direct fallback or service restart is performed.

## State, sessions and observation

The default state root is `~/.local/state/crosscrew`. Override it with
`CROSSCREW_STATE_DIR`; the client and service must use the same root. Credentials
stay in provider-managed native authentication stores. Crosscrew stores its own
broker token, prompts, outputs and session metadata locally.

`job start` returns a job ID and spawns a supervised process. Provider session IDs
are recorded when available. A follow-up uses the same run ID, provider and profile
with `--mode resume`; implicit resume validates the stored target/profile/age
(older records lack target checks; explicit session-ID imports bypass registry checks). Use the same target and include the changed context in the
new brief. Resume depends on native provider session retention. State files cannot
recreate a provider session after it expires. Concurrent requests must not reuse
a session as if they were independent reviewers.

`wait` observes without mutating state. The result reference reports path, byte
size and SHA-256, including pending results. A terminal status without a result is
possible. `status` and `collect` can reconcile state and have write effects.
`--summary` returns an allowlisted, UTF-8-bounded answer (8 KiB), session and usage
from the same hashed bytes. `wait-many` observes up to 16 jobs in one process with
one terminal/timeout JSON response. Wait timeouts and waiter termination do not cancel the worker. Cancellation is an
explicit `job cancel`. Progress events only retain allowlisted metadata; brief,
stdout and stderr files still contain task content and must remain private.

The broker contract hash covers its loaded modules and active registry config.
After updating code/config, explicitly restart an installed service. A stale
broker fails closed. Changing native CLI versions can still break invocation;
a matching hash does not verify provider compatibility.

## Work runtime boundaries

Claude work remains prompt-guarded; Codex work uses its native workspace-write
sandbox. Grok work requires a separately installed `crosscrew-target-v1` profile
extending strict, with opt-in, specific read-only runtime directories. No personal
Python path is shipped. Before work, a recognized expiring Grok login may be
refreshed using its native model-list command; credentials are never exported.

agy work creates a temporary target project. Exact command grants, when requested,
are escaped/anchored and require outer OS write confinement. Setup failure,
completion and cancellation release them; orphan cleanup is limited to the
`crosscrew-work-*` namespace. Host-native approval of a headless start/wait command
is separate from worker grants and is never installed globally.

Seatbelt permits provider state and system temp writes in addition to a work
target. A review target inside those exceptions cannot be protected; the envelope
reports that warning. On unsupported OSes the wrapper reports weaker prompt guards;
exact agy command grants fail closed. This is write confinement, not read/network
isolation. See [work-runtime.md](docs/work-runtime.md).

## Usage and acceptance

Provider-reported usage is allowlisted separately from answer text. Claude reports
per invocation; Codex/Grok terminal counters are treated as session cumulative;
Gemini scope remains unverified and is excluded from sums. Missing counts remain
null. The read-only summary consumes explicit schema 2.0 envelopes; a separate
manifest can compare independently accepted Codex-host arms. Neither tool measures
account quota or automatically captures host preparation/review. See
[usage-accounting.md](docs/usage-accounting.md).

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
