# Using Crosscrew

[← Back to Crosscrew](../README.md) · [Calling card](../quick-contract.md) · [Architecture](../architecture.md)

## Install from source

Install and log in to the native CLIs you want to use, using each provider's own
instructions. You only need the providers you will actually call. Crosscrew neither
installs them nor copies credentials. Ensure `python3 --version` is **3.11 or newer**.

```bash
git clone https://github.com/ktaey7/crosscrew.git "$HOME/.local/share/crosscrew"
cd "$HOME/.local/share/crosscrew"
python3 install.py --dry-run --host claude --host codex
python3 install.py --host claude --host codex
```

The installer creates a launcher at `~/.local/bin/crosscrew` and the explicitly
selected, named Crosscrew entrypoints. It refuses to overwrite unrelated files.
Make `~/.local/bin` available on your shell PATH if it is not already there.
Alternatively run `python3 /absolute/path/to/crosscrew/crosscrew.py` directly.
The installed entrypoints use the selected Python executable and absolute path.
Keep the source checkout in place while installed.

`--host claude` adds `~/.claude/commands/crosscrew.md`; `--host codex` adds
`~/.codex/skills/crosscrew/SKILL.md`; experimental `--host grok` adds
`~/.grok/skills/crosscrew/SKILL.md`. Select any subset, or omit all hosts for a CLI-only
installation. No global instructions, hooks, MCPs, credentials or existing Council
files are replaced. Host discovery after installation may require a new session.
For agy, manually provide the calling card and `--host agy`; there is no automatic
edit to `GEMINI.md` in this release.

```bash
crosscrew doctor --providers claude codex
crosscrew call codex --host claude --check
```

Doctor checks availability and known API-auth overrides without calling a model.
`checks_passed` does **not** mean authentication, billing or a real task was verified.
A missing broker is reported separately because direct routes do not need one.

## First review, then follow-up

In a host with the entrypoint installed, try:

> Use Crosscrew to ask Codex to challenge this plan. Keep the review session so we
> can send a revised plan back to the same reviewer.

The host should write a focused brief, start the job, retain its ID and wait for the
result. The equivalent CLI flow is:

```bash
# Substitute absolute paths for your own project and briefs.
crosscrew job start codex /absolute/path/review.md \
  --host claude --target /absolute/path/project \
  --profile review --mode fresh --run-id design-review
crosscrew job wait JOB_ID_FROM_START

# After evaluating the result and revising the plan:
crosscrew job start codex /absolute/path/follow-up.md \
  --host claude --target /absolute/path/project \
  --profile review --mode resume --run-id design-review
crosscrew job wait SECOND_JOB_ID
```

The actual host must be supplied; `--host shell` is for a real ordinary terminal,
not a workaround for another host's sandbox. Briefs carry the necessary context;
Crosscrew does not copy the entire host conversation. Results include session
metadata when the provider supplies it. `wait` returns a result reference with
path, size and SHA-256; the host reads and verifies that file. The file may be
`result.pending.json` before collection. Waiting can time out without canceling
the worker. Reconnect with the same job ID instead of starting a duplicate.

See `crosscrew card` and `crosscrew job --help`. Work/research profiles exist but
require the corresponding user scope; a successful worker response is not a
substitute for host verification.

## Optional broker for sandboxed hosts

Some routes, including Codex → Claude, require a broker outside the host sandbox.
The broker can start a native CLI with your account's permissions. It validates
typed requests and authenticates a loopback connection, but does not add an OS
sandbox to Claude. It is not a remote server or a multi-user security boundary.

Start it manually in an ordinary terminal:

```bash
crosscrew broker serve
```

Or explicitly install the macOS user service from that terminal:

```bash
crosscrew broker render     # inspect the exact LaunchAgent first
crosscrew broker install
crosscrew broker health
```

Ordinary installation never enables this service. Crosscrew uses its own label,
`io.github.ktaey7.crosscrew.broker`, and its own state directory. Loopback must be
reachable from the host; Crosscrew does not change host network permissions.
The service stores the installation-time PATH and Crosscrew configuration paths,
not shell API tokens. Reinstall it explicitly when those paths change. Native CLI
configuration and authentication still apply in the service process.

## Authentication and billing

Crosscrew is intended for people who already use supported native CLIs through
their provider accounts. It invokes those CLIs; it does not exchange OAuth tokens
for API access, proxy a subscription, or silently fall back to an API.

**Native CLI execution alone does not prove subscription billing.** CLI settings,
saved authentication, environment variables, plan limits and extra usage affect
billing. This alpha blocks known API/cloud environment overrides, Claude
`apiKeyHelper`/settings overrides and selected Codex custom-provider settings.
It never prints their values. This is a partial check, not a complete subscription
billing detector. Verify the native CLI's active authentication and usage settings
before your first real call. The guard deliberately blocks known API variables
even if they appear to belong to another provider; use a clean environment.

Claude's noninteractive authentication precedence is documented in its
[official authentication guide](https://code.claude.com/docs/en/authentication#authentication-precedence).
No fixed prices, unlimited use or compatibility with future CLI versions are promised.

## Update and remove

Do not update while jobs are running. Use `crosscrew job list --running`, finish or
explicitly cancel those jobs, then update this source checkout:

```bash
git pull --ff-only
python3 install.py --host claude --host codex  # refresh only the hosts you installed
crosscrew broker restart                    # only if you installed the service
crosscrew doctor --providers claude codex
```

For a tagged version, inspect release notes and check out its tag instead. This
alpha has no automatic updater or state migrations. The broker rejects stale
contracts; restart it after updates. Keep a private backup of runtime state before
changing versions. A code rollback does not roll back provider sessions.

To remove your selected entrypoints and launcher:

```bash
crosscrew broker uninstall                  # only if you installed the service
python3 install.py --uninstall --host claude --host codex
```

State lives at `~/.local/state/crosscrew` by default and is retained on uninstall.
It contains briefs, outputs, session identifiers and broker credentials; keep it
private and out of Git. `CROSSCREW_STATE_DIR` overrides it. Advanced users can
supply a complete `CROSSCREW_BACKENDS_CONFIG` JSON file; use the same absolute
configuration path for the host and broker. No `MULTIAI_*` variables or state from
the original personal setup are reused.

