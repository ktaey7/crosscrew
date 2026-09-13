# Work profiles and native runtimes

[Usage](usage.md) · [Host approvals](host-approvals.md) · [Architecture](../architecture.md)

A work brief authorizes specific edits in an absolute target. It does not authorize
publishing, credentials, system changes or unrelated files. The host must review
the diff and evidence before accepting the result. Native settings still apply.

## Grok: explicitly install a strict profile

Review/research use Grok's native read-only profile. Work requires the named
`crosscrew-target-v1` profile extending `strict`; a missing or conflicting profile
fails before launching the worker. Crosscrew does not install it automatically.

First inspect the proposed native configuration:

```bash
crosscrew grok-sandbox --render
crosscrew grok-sandbox --install
crosscrew grok-sandbox
```

`--install` explicitly appends to `$GROK_HOME/sandbox.toml` (default
`~/.grok/sandbox.toml`). Existing profiles and comments are preserved. A different
profile under the same name is refused, not overwritten. User-configured symlink
paths are refused. The standard macOS `/tmp` and `/var` aliases are accepted.

No interpreter or package-manager directories are granted by default. If a work
task needs an executable/runtime outside the target and Grok denies access, inspect
that denial and select the smallest required runtime directories. Do not grant a
whole home, disk, `/usr`, or Homebrew prefix. Reads are separate from write access.

To configure an explicit runtime, copy the full shipped `backends.json` to a
private configuration file, set `CROSSCREW_BACKENDS_CONFIG` to its absolute path,
and edit only this section under `providers.grok`:

```json
"work_sandbox": {
  "profile": "crosscrew-target-v1",
  "runtime_read_paths": ["/absolute/path/to/a/specific/runtime/version"]
}
```

The example path must be replaced with an existing directory. Paths are validated
and only become `read_only` grants. Run `--render` and explicitly install after
reviewing them. If that named profile already exists with different contents,
review and update only that profile manually; Crosscrew refuses to widen it.
Client and broker must use the same configuration. Restart the public broker
explicitly after finishing active jobs; reinstall an optional LaunchAgent when its
configuration path or PATH changes. Personal delegation services are unrelated.

The strict worker cannot refresh its global login cache. When Crosscrew observes a
recognized native JWT cache expiring within five minutes, it invokes `grok models`
once before entering the work sandbox, discarding output. This is a native
model-list/authentication request, not a generation request; native authentication
may update its own cache. Crosscrew reads the expiry locally, never prints or
exports credentials, and implements no OAuth exchange. A failed refresh stops the
worker. Unknown cache formats and expiry during long-running work are not repaired.

## Gemini/Antigravity: exact command grants

All three modes are supported: `oneshot`, `fresh`, `resume`. The adapter parses the
native JSON response and observed conversation ID. Missing IDs in fresh/resume,
mismatched returned IDs and native tool denials are failures, even if a partial
answer is present. This adapter targets `agy`, not a different `gemini` CLI.

For authorized work, Crosscrew creates a temporary native project containing the
target write grant. A terminal command that must run headlessly may be passed
explicitly and repeated for several commands:

```bash
crosscrew job start agy /absolute/path/work.md \
  --host claude --target /absolute/path/project --profile work \
  --mode fresh --run-id repair-example \
  --allow-command 'python3 -m unittest -q'
crosscrew job wait JOB_ID --summary
```

Use the exact command needed for your project. Each command becomes an escaped,
anchored native command and unsandboxed permission rule. No prefix or wildcard
permission is generated. There are at most 16 commands, each at most 4096
characters, with line/control characters rejected. Exact matching limits *which
command* is approved; it does not prove that command is safe. Its scripts, imports,
reads and network activity still need appropriate task scope.

Command grants require the outer macOS OS write sandbox. That wrapper permits
writes to the target and provider state/temp exceptions; it is not read or network
isolation. Unsupported platforms refuse `--allow-command`. See the architecture
for weaker behavior when no command grants are requested.

Temporary files use `crosscrew-work-*`, are locked while active, and are removed
on completion, cancellation or setup failure. Orphan cleanup only examines that
public namespace. Old `multi-ai-work-*` files and unrelated native projects are
not adopted or deleted. Never copy a working grant into global permission settings.
